import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.application.evidence import EvidenceRegistry
from app.application.retriever import RetrieverService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, RunContext, SearchHit
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import FallbackModels, Models, SQL, ScriptedModel, Web, call, final


def plan(queries=None, **changes):
    return AIMessage(content=json.dumps({"route": "retrieve", "queries": queries or ["NX-1 功率"], **changes}, ensure_ascii=False))


def grade(route="answer", *, numbers=None, **changes):
    value = {"route": route, "relevance": "strong", "answerability": "sufficient" if route == "answer" else "partial",
             "ambiguity": "none", "supported_numbers": numbers if numbers is not None else ([1] if route == "answer" else []),
             "summary": "额定功率100W[1]" if route == "answer" else "缺少适用购买日期的保修条款。"}
    return AIMessage(content=json.dumps({**value, **changes}, ensure_ascii=False))


class Retriever:
    def __init__(self, results=None):
        self.calls = []
        self.results = results

    def retrieve(self, uid, query, dids, *, deadline=None):
        self.calls.append((uid, query, dids, deadline))
        if self.results is not None:
            value = self.results[len(self.calls) - 1]
            if isinstance(value, Exception):
                raise value
            if value is None:
                return []
            text = value
        else:
            text = "NX-1 额定功率100W"
        return [SearchHit(Chunk(str(uuid4()), str(uid), str(dids[0]), len(self.calls), text, "产品手册.md"), .03)]


def setup(responses, *, settings=None, retriever=None, models=None, mode="knowledge", **context_changes):
    model = ScriptedModel(responses=responses)
    context = RunContext(request_id="quality-test", user_id=uuid4(), session_id=uuid4(), question="NX-1 的功率与保修政策？",
                         search_mode=mode, allowed_document_ids=(uuid4(),), selected_provider="deepseek", public_query="公开信息",
                         web_enabled=mode == "auto")
    context = replace(context, **context_changes)
    retriever = retriever or Retriever()
    workflow = SupervisorWorkflow(settings or Settings(_env_file=None, knowledge_quality_enabled=True), retriever,
                                  models or Models(model), Web(), sql=SQL())
    return workflow, context, model, retriever


def test_simple_plan_grades_once_and_uses_shared_runtime_and_scope():
    workflow, ctx, model, retriever = setup([call("delegate_rag", task="NX-1 功率"), plan(), grade(), final("功率100W[1]")])
    events = []
    result = workflow.run(ctx, [], "", lambda event, value: events.append((event, value)))
    assert result.run_metadata["model_attempts"] == 4
    assert result.run_metadata["knowledge_searches"] == 1
    assert result.run_metadata["knowledge_events"][0]["status"] == "ok"
    assert result.run_metadata["knowledge_rewrites"] == 0
    assert retriever.calls[0] == (ctx.user_id, "NX-1 功率", list(ctx.allowed_document_ids), ctx.deadline_monotonic)
    assert [item["number"] for item in result.citations] == [1]
    assert [event for event, value in events if event.startswith("agent_")] == ["agent_started", "agent_finished"]
    streamed = "".join(value for event, value in events if event == "answer_delta")
    assert streamed == "功率100W[1]"
    assert "queries" not in streamed and "supported_numbers" not in streamed


def test_complex_plan_queries_each_fact_and_keeps_unified_numbering():
    workflow, ctx, model, retriever = setup([
        call("delegate_rag", task="NX-1 功率和2024购买产品的保修"), plan(["NX-1 额定功率", "NX-1 2024购买 保修政策"]),
        grade(numbers=[1, 2], summary="功率100W[1]；2024年购买产品保修两年[2]"), final("功率100W[1]，保修两年[2]"),
    ], retriever=Retriever(["NX-1 额定功率100W", "NX-1 2024购买的产品保修两年"]))
    result = workflow.run(ctx, [], "")
    assert [item[1] for item in retriever.calls] == ["NX-1 额定功率", "NX-1 2024购买 保修政策"]
    assert result.run_metadata["tool_requests"] == 2
    assert len(result.citations) == 2
    assert [source["number"] for source in result.citations] == [1, 2]
    assert "context_only" not in str(model.seen_calls[2])


def test_partial_evidence_rewrites_once_then_combines_original_and_new_sources():
    workflow, ctx, _, retriever = setup([
        call("delegate_rag", task="NX-1 功率与2024购买的保修"), plan(),
        grade("rewrite", numbers=[1], rewritten_query="NX-1 2024购买 保修期限"),
        grade(numbers=[1, 2], summary="功率100W[1]，保修两年[2]"), final("功率100W[1]，保修两年[2]"),
    ], retriever=Retriever(["功率100W", "2024购买的NX-1保修两年"]))
    result = workflow.run(ctx, [], "")
    assert [item[1] for item in retriever.calls] == ["NX-1 功率", "NX-1 2024购买 保修期限"]
    assert result.run_metadata["knowledge_rewrites"] == 1
    assert result.run_metadata["model_attempts"] == 5
    assert result.run_metadata["excluded_source_numbers"] == []


def test_rewrite_remains_insufficient_and_cannot_cite_nonempty_candidates():
    workflow, ctx, model, retriever = setup([
        call("delegate_rag", task="查询保修"), plan(),
        grade("rewrite", rewritten_query="NX-1 2024购买 保修期限"),
        grade("rewrite", rewritten_query="NX-1 2024购买 售后保障"),
        final("资料不足，请补充保修条款。", "insufficient"),
    ])
    result = workflow.run(ctx, [], "")
    assert len(retriever.calls) == 2
    assert result.citations == [] and result.kind == "insufficient"
    assert result.run_metadata["warnings"] == ["RAG_REWRITE_LIMIT_REACHED"]
    assert result.run_metadata["excluded_source_numbers"] == [1, 2]
    assert '"sources": []' in str(model.seen_calls[-1])


def test_planner_asks_for_missing_conditions_without_search_or_grading():
    workflow, ctx, model, retriever = setup([
        call("delegate_rag", task="它的保修呢"), plan(route="clarify", queries=[], clarification="请提供型号和购买日期。"),
        final("请提供型号和购买日期。", "clarification"),
    ])
    result = workflow.run(ctx, [], "")
    assert result.kind == "clarification" and not retriever.calls
    assert result.run_metadata["model_attempts"] == 3


def test_grader_ambiguity_does_not_trigger_rewrite_even_if_query_is_present():
    workflow, ctx, _, retriever = setup([
        call("delegate_rag", task="保修期限"), plan(),
        grade("rewrite", ambiguity="missing_slot", summary="请提供购买年份。", rewritten_query="NX-1 保修"),
        final("请提供购买年份。", "clarification"),
    ])
    result = workflow.run(ctx, [], "")
    assert len(retriever.calls) == 1 and result.run_metadata["knowledge_rewrites"] == 0
    assert result.kind == "clarification" and result.citations == []


def test_no_hits_does_not_invoke_grader_or_invent_rewrite():
    workflow, ctx, _, retriever = setup([call("delegate_rag", task="查手册"), plan(), final("未找到资料。", "insufficient")],
                                       retriever=Retriever([None]))
    result = workflow.run(ctx, [], "")
    assert result.run_metadata["model_attempts"] == 3
    assert result.run_metadata["knowledge_searches"] == 1
    assert result.run_metadata["knowledge_events"][0]["grades"] == []


@pytest.mark.parametrize("invalid", [AIMessage(content="not json"), plan(["a", "b", "c"]), plan(["a", "a"]),
                                    plan([" "]), plan(route="clarify", queries=[], clarification="问什么[99]？")])
def test_invalid_plan_fails_without_retrieving(invalid):
    workflow, ctx, _, retriever = setup([call("delegate_rag", task="查询"), invalid, final("任务规划失败，请重新描述。", "insufficient")])
    result = workflow.run(ctx, [], "")
    assert not retriever.calls
    assert result.run_metadata["failures"][0]["code"] == "RAG_PLAN_INVALID"


@pytest.mark.parametrize("invalid", [AIMessage(content="not json"), grade(numbers=[99]), grade(numbers=[1, 1]),
                                    grade(numbers=[True]), grade(answerability="partial"), grade(summary="功率[99]"),
                                    grade("rewrite", rewritten_query="NX-1 功率"), grade("rewrite"),
                                    grade("no_knowledge", numbers=[1], summary="未找到[1]")])
def test_invalid_grader_never_approves_candidates(invalid):
    workflow, ctx, _, _ = setup([call("delegate_rag", task="查询"), plan(), invalid, final("证据判断失败。", "insufficient")])
    result = workflow.run(ctx, [], "")
    assert not result.citations
    assert result.run_metadata["failures"][0]["code"] == "RAG_GRADE_INVALID"
    assert result.run_metadata["excluded_source_numbers"] == [1]


def test_rejected_citation_is_repaired_with_only_eligible_evidence():
    workflow, ctx, model, _ = setup([
        call("delegate_rag", task="查保修"), plan(), grade("no_knowledge", summary="只有功率，没有保修政策。"),
        final("保修一年[1]"), final("没有找到保修政策。", "insufficient"),
    ])
    result = workflow.run(ctx, [], "")
    assert result.run_metadata["repair_used"]
    repair = model.seen_calls[-1][-1].content
    assert "本轮证据：[]" in repair and "有效来源编号：[1]" not in repair
    assert result.citations == []


def test_sql_plus_rag_retains_only_approved_knowledge_and_sql_numbering():
    workflow, ctx, _, _ = setup([
        call("delegate_sql", task="统计销量"), call("describe_sql_schema"),
        call("execute_sql", query="SELECT SUM(amount) FROM sales.orders"),
        AIMessage(content='{"status":"ok","summary":"销量42[1]"}'),
        call("delegate_rag", task="型号功率"), plan(), grade(numbers=[2], summary="功率100W[2]"), final("销量42[1]；功率100W[2]"),
    ], mode="auto")
    result = workflow.run(ctx, [], "")
    assert [source["type"] for source in result.citations] == ["sql", "knowledge"]
    assert result.run_metadata["model_attempts"] == 8


def test_partial_mixed_task_can_use_sql_while_explaining_rag_gap():
    workflow, ctx, _, _ = setup([
        call("delegate_sql", task="统计销量"), call("describe_sql_schema"),
        call("execute_sql", query="SELECT SUM(amount) FROM sales.orders"), AIMessage(content='{"status":"ok","summary":"销量42[1]"}'),
        call("delegate_rag", task="保修政策"), plan(), grade("no_knowledge", summary="没有适用保修政策。"),
        final("销量42[1]；知识库缺少保修政策，无法完成保修分析。"),
    ], mode="auto")
    result = workflow.run(ctx, [], "")
    assert [source["number"] for source in result.citations] == [1]
    assert result.run_metadata["excluded_source_numbers"] == [2]


def test_whole_request_shares_one_rewrite_across_rag_delegations():
    workflow, ctx, _, retriever = setup([
        call("delegate_rag", task="查功率"), plan(), grade("rewrite", rewritten_query="NX-1 额定功率规格"), grade(),
        call("delegate_rag", task="再查保修"), plan(["NX-1 保修"]), grade("rewrite", rewritten_query="NX-1 2024 保修期限"),
        final("功率100W[1]；保修资料不足。"),
    ])
    workflow.settings = Settings(_env_file=None, knowledge_quality_enabled=True, agent_max_knowledge_calls=6)
    result = workflow.run(ctx, [], "")
    assert len(retriever.calls) == 3
    assert result.run_metadata["knowledge_rewrites"] == 1
    assert result.run_metadata["knowledge_events"][1]["error_code"] == "RAG_REWRITE_LIMIT_REACHED"
    assert result.citations[0]["number"] == 1


def test_search_budget_prevents_rewrite_without_bypassing_shared_limits():
    workflow, ctx, _, retriever = setup([
        call("delegate_rag", task="查功率和保修"), plan(["NX-1 功率", "NX-1 保修"]),
        grade("rewrite", rewritten_query="NX-1 2024 保修期限"), final("证据不足，检索预算已用尽。", "insufficient"),
    ], settings=Settings(_env_file=None, knowledge_quality_enabled=True, agent_max_knowledge_calls=2))
    result = workflow.run(ctx, [], "")
    assert len(retriever.calls) == 2
    assert result.run_metadata["warnings"] == ["RAG_SEARCH_LIMIT_REACHED"]


def test_planner_and_grader_consume_the_existing_model_budget():
    workflow, ctx, model, retriever = setup([call("delegate_rag", task="查功率"), plan(), grade()],
        settings=Settings(_env_file=None, knowledge_quality_enabled=True, agent_max_model_calls=2))
    with pytest.raises(AppError) as error:
        workflow.run(ctx, [], "")
    assert error.value.code == "AGENT_LIMIT_EXCEEDED" and model.position == 2


def test_retrieval_failure_is_not_recorded_as_no_knowledge():
    workflow, ctx, _, _ = setup([call("delegate_rag", task="查手册"), plan(), final("知识库暂时不可用。", "insufficient")],
                               retriever=Retriever([AppError("MILVUS_UNAVAILABLE", "unavailable", 503)]))
    result = workflow.run(ctx, [], "")
    assert result.run_metadata["knowledge_events"][0]["status"] == "failed"
    assert result.run_metadata["knowledge_events"][0]["error_code"] == "MILVUS_UNAVAILABLE"
    assert result.run_metadata["agent_events"][0]["status"] == "failed"


def test_fallback_during_grading_does_not_repeat_retrieval_or_planning():
    primary = ScriptedModel(responses=[call("delegate_rag", task="查功率"), plan(), httpx.ConnectError("unavailable")])
    fallback = ScriptedModel(responses=[grade(), final("功率100W[1]")])
    workflow, ctx, _, retriever = setup([], models=FallbackModels(primary, fallback))
    result = workflow.run(ctx, [], "")
    assert len(retriever.calls) == 1
    assert result.run_metadata["fallback_used"] and result.run_metadata["model_attempts"] == 5


def test_no_quality_state_is_shared_between_requests():
    sequence = [call("delegate_rag", task="查功率"), plan(), grade(), final("功率100W[1]")]
    workflow, ctx, _, _ = setup(sequence + sequence)
    first = workflow.run(ctx, [], "")
    second = workflow.run(replace(ctx, user_id=uuid4(), session_id=uuid4()), [], "")
    assert first.run_metadata["knowledge_events"] is not second.run_metadata["knowledge_events"]
    assert len(second.run_metadata["knowledge_events"]) == 1
    assert [citation["number"] for citation in second.citations] == [1]


def test_registry_excludes_failed_grade_without_losing_other_source_types():
    registry = EvidenceRegistry()
    uid, did = uuid4(), uuid4()
    registry.add_knowledge([SearchHit(Chunk(str(uuid4()), str(uid), str(did), 0, "未通过审核", "manual.md"), .03)])
    registry.excluded_numbers.add(1)
    assert registry.validate("clarification", "请明确产品版本。") == []
    with pytest.raises(AppError):
        registry.validate("grounded", "事实[1]")


def test_real_retriever_scope_failure_cannot_be_approved_by_grader():
    forbidden = SearchHit(Chunk(str(uuid4()), str(uuid4()), str(uuid4()), 0, "其他用户的文档", "private.md"), .03)
    retriever = RetrieverService(SimpleNamespace(embed_query=lambda query: [0.]),
                                 SimpleNamespace(search=lambda *args: [forbidden]), 6)
    workflow, ctx, model, _ = setup([call("delegate_rag", task="查手册"), plan(), final("文档检索失败。", "insufficient")],
                                   retriever=retriever)
    result = workflow.run(ctx, [], "")
    assert result.run_metadata["knowledge_events"][0]["error_code"] == "VECTOR_SCOPE_MISMATCH"
    assert result.citations == []
    assert "其他用户的文档" not in str(model.seen_calls)


def test_deadline_expiring_during_retrieval_stops_before_grader():
    clock = [0.]
    retriever = Retriever()
    original = retriever.retrieve

    def expire(*args, **kwargs):
        clock[0] = 2.
        return original(*args, **kwargs)

    retriever.retrieve = expire
    workflow, ctx, model, _ = setup([call("delegate_rag", task="查手册"), plan()], retriever=retriever,
                                   deadline_monotonic=1.)
    with patch("app.workflows.agent.monotonic", side_effect=lambda: clock[0]), patch("app.workflows.supervisor.monotonic", side_effect=lambda: clock[0]):
        with pytest.raises(AppError) as error:
            workflow.run(ctx, [], "")
    assert error.value.code == "AGENT_TIMEOUT" and model.position == 2


def test_later_grade_failure_does_not_revoke_a_previously_approved_duplicate():
    uid, did, cid = uuid4(), uuid4(), str(uuid4())
    hit = SearchHit(Chunk(cid, str(uid), str(did), 0, "功率100W", "spec.md"), .03)
    retriever = SimpleNamespace(retrieve=lambda *args, **kwargs: [hit])
    workflow, ctx, _, _ = setup([
        call("delegate_rag", task="查功率"), plan(), grade(),
        call("delegate_rag", task="再查保修"), plan(["NX-1 保修"]), grade("no_knowledge", summary="没有保修政策。"),
        final("功率100W[1]；保修资料不足。"),
    ], retriever=retriever, user_id=uid, allowed_document_ids=(did,))
    result = workflow.run(ctx, [], "")
    assert result.run_metadata["excluded_source_numbers"] == []
    assert len(result.citations) == 1
    assert result.run_metadata["knowledge_events"][1]["status"] == "empty"
