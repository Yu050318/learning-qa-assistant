"""Exercise the actual Supervisor graph against persistent call receipts."""
from datetime import timedelta
from dataclasses import replace
from uuid import uuid4
from langchain_core.messages import AIMessage

from app.application.run_checkpoint import RunCheckpoint
from app.domain.contracts import RunContext
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, SQL, Web, call, final, report
from test_run_control import control, system


class CountSQL(SQL):
    descriptions = 0
    queries = 0

    def schema(self, remaining):
        self.descriptions += 1
        return super().schema(remaining)

    def query(self, query, remaining):
        self.queries += 1
        return super().query(query, remaining)


def context(s):
    return RunContext(request_id="checkpoint-graph", user_id=s.uid, session_id=s.sid,
        question="查询汇总", search_mode="sql", allowed_document_ids=(), selected_provider="deepseek",
        public_query=None, web_enabled=False)


def test_recovery_reuses_completed_models_tools_and_evidence(control):
    s = control
    responses = [call("delegate_sql", task="汇总"), call("describe_sql_schema"),
        call("execute_sql", query="SELECT sum(amount) AS total FROM sales.orders"), report(), final("总计42[1]")]
    model, sql = ScriptedModel(responses=responses), CountSQL()
    workflow = SupervisorWorkflow(s.settings, None, Models(model), Web(), sql=sql)
    first = workflow.run(context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert model.position == 5 and sql.descriptions == sql.queries == 1
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    # An empty script makes any accidental upstream inference fail immediately.
    resumed_model = ScriptedModel(responses=[])
    resumed = SupervisorWorkflow(s.settings, None, Models(resumed_model), Web(), sql=sql)
    second = resumed.run(context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.answer.content == first.answer.content and second.citations == first.citations
    assert second.run_metadata["model_attempts"] == first.run_metadata["model_attempts"] == 5
    assert sql.descriptions == sql.queries == 1 and resumed_model.position == 0


def test_human_resume_uses_previous_graph_messages_and_shared_budget(control):
    s = control
    claim = s.store.claim_next()
    checkpoint = RunCheckpoint(s.store, claim)
    model = ScriptedModel(responses=[final("请补充地区", "clarification")])
    first = SupervisorWorkflow(s.settings, None, Models(model), Web()).run(context(s), [], "", checkpoint=checkpoint)
    assert first.kind == "clarification"
    paused = s.store.pause(claim, checkpoint.state, first.answer.content)
    s.store.resume(s.uid, s.run_id, 1, paused.resume_key, "境内")
    resumed_model = ScriptedModel(responses=[final("已收到境内范围", "smalltalk")])
    second = SupervisorWorkflow(s.settings, None, Models(resumed_model), Web()).run(
        context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.run_metadata["model_attempts"] == 2
    messages = resumed_model.seen_calls[0]
    assert any("请补充地区" in str(message.content) for message in messages)
    assert "境内" in messages[-1].content


def test_multiple_delegations_keep_evidence_order_on_recovery(control):
    s = control
    root = AIMessage(content="", tool_calls=[*call("delegate_sql", task="第一项").tool_calls,
                                             *call("delegate_sql", task="第二项").tool_calls])
    responses = [root, call("describe_sql_schema"), call("execute_sql", query="SELECT 1"), report("第一项[1]"),
        call("describe_sql_schema"), call("execute_sql", query="SELECT 2"), report("第二项[2]"), final("42[1]，42[2]")]
    model, sql = ScriptedModel(responses=responses), CountSQL()
    first = SupervisorWorkflow(s.settings, None, Models(model), Web(), sql=sql).run(
        context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert model.position == 8 and sql.queries == 2
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    resumed_model = ScriptedModel(responses=[])
    second = SupervisorWorkflow(s.settings, None, Models(resumed_model), Web(), sql=sql).run(
        context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.citations == first.citations and second.answer.content == first.answer.content
    assert sql.queries == 2 and resumed_model.position == 0


def test_quality_planner_grader_and_search_reuse_confirmed_receipts(control):
    from test_knowledge_quality import Retriever, plan, grade
    s = control
    s.settings.knowledge_quality_enabled = True
    ctx = replace(context(s), search_mode="knowledge", allowed_document_ids=(uuid4(),))
    retriever = Retriever()
    model = ScriptedModel(responses=[call("delegate_rag", task="NX-1 功率"), plan(), grade(), final("功率100W[1]")])
    first = SupervisorWorkflow(s.settings, retriever, Models(model), Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert model.position == 4 and len(retriever.calls) == 1
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    resumed = ScriptedModel(responses=[])
    second = SupervisorWorkflow(s.settings, retriever, Models(resumed), Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.answer.content == first.answer.content and second.citations == first.citations
    assert second.run_metadata["knowledge_searches"] == 1 and second.run_metadata["model_attempts"] == 4
    assert resumed.position == 0 and len(retriever.calls) == 1


def test_provider_fallback_is_replayed_without_repeating_failed_or_successful_calls(control):
    from app.core.errors import UpstreamError
    from test_agent_v2 import FallbackModels
    s = control
    primary = ScriptedModel(responses=[UpstreamError("llm", retryable=True)])
    fallback = ScriptedModel(responses=[final("备用模型的回答", "smalltalk")])
    first = SupervisorWorkflow(s.settings, None, FallbackModels(primary, fallback), Web()).run(
        context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert primary.position == fallback.position == 1
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    primary, fallback = ScriptedModel(responses=[]), ScriptedModel(responses=[])
    second = SupervisorWorkflow(s.settings, None, FallbackModels(primary, fallback), Web()).run(
        context(s), [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.answer.content == first.answer.content and second.answer.provider == "ollama"
    assert second.run_metadata["model_attempts"] == 2 and primary.position == fallback.position == 0
