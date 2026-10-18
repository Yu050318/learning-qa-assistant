import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.application.retriever import RetrieverService
from app.domain.contracts import Chunk, SearchHit
from app.workflows.knowledge import QueryRewrite
from test_agent_v2 import call, final
from test_knowledge_quality import Retriever, grade, plan, setup
from test_run_control import control, system


@pytest.mark.parametrize("method", ["step_back", "hyde"])
def test_named_rewrite_preserves_user_constraints_and_cites_only_real_chunks(method):
    class TracedRetriever(Retriever):
        hints = []

        def retrieve(self, *args, embedding_query=None, **kwargs):
            self.hints.append(embedding_query)
            return super().retrieve(*args, **kwargs)

    rewrite = {"rewrite_method": method, "rewritten_query": "NX-1 2024 保修期限",
               "step_back_question": "购买日期如何影响售后期限" if method == "step_back" else "",
               "hyde_document": "HYPOTHETICAL_ONLY_999YEAR：假设保修999年" if method == "hyde" else ""}
    retriever = TracedRetriever(["NX-1 功率100W", "境内2024年购买的NX-1保修两年"])
    workflow, ctx, model, _ = setup([call("delegate_rag", task="NX-1 2024 保修"), plan(),
        grade("rewrite", rewritten_query="NX-1 2024 保修期限", rewrite_method=method),
        AIMessage(content=json.dumps(rewrite, ensure_ascii=False)),
        grade(numbers=[2], summary="2024年购买保修两年[2]"), final("2024年购买保修两年[2]")],
        retriever=retriever, question="NX-1 境内2024年购买的保修期限？")
    evidence = []
    result = workflow.run(ctx, [], "", on_evidence=lambda rows: evidence.extend(rows))
    assert result.answer.content == "2024年购买保修两年[2]" and model.position == 6
    assert ctx.question in retriever.calls[1][1] and "NX-1 2024 保修期限" in retriever.calls[1][1]
    assert result.run_metadata["knowledge_events"][0]["rewrite"]["rewrite_method"] == method
    assert result.run_metadata["knowledge_rewrites"] == 1
    assert len(evidence) == 1 and "HYPOTHETICAL_ONLY_999YEAR" not in str(evidence)
    assert "HYPOTHETICAL_ONLY_999YEAR" not in str(result.citations)
    assert "HYPOTHETICAL_ONLY_999YEAR" not in str(model.seen_calls[-2:])
    assert (retriever.hints[1] is not None) == (method == "hyde")


def test_hyde_changes_dense_embedding_without_changing_sparse_query_or_document_scope():
    uid, did, chunk_id = uuid4(), uuid4(), uuid4()
    embedded, searched = [], []
    hit = SearchHit(Chunk(str(chunk_id), str(uid), str(did), 0, "真实资料", "fixture.md"), .8)

    def search(user, ids, query, vector, top_k):
        searched.append((user, ids, query, vector))
        return [hit]

    service = RetrieverService(SimpleNamespace(embed_query=lambda query: embedded.append(query) or [1.]),
                               SimpleNamespace(search=search), 3)
    assert service.retrieve(uid, "NX-1 2024 保修", [did], embedding_query="假设匹配文本") == [hit]
    assert embedded == ["假设匹配文本"]
    assert searched == [(uid, [did], "NX-1 2024 保修", [1.])]


@pytest.mark.parametrize("method,fields", [("step_back", {}), ("hyde", {}),
    ("step_back", {"step_back_question": "原则", "hyde_document": "假设"}),
    ("hyde", {"step_back_question": "原则", "hyde_document": "假设"})])
def test_rewrite_method_requires_exactly_its_own_bounded_payload(method, fields):
    with pytest.raises(ValidationError):
        QueryRewrite.model_validate({"rewrite_method": method, "rewritten_query": "缺失事实", **fields})


@pytest.mark.parametrize("method", ["step_back", "hyde"])
def test_recovery_reuses_rewrite_generation_and_both_retrievals(control, method):
    from dataclasses import replace
    from datetime import timedelta
    from app.application.run_checkpoint import RunCheckpoint
    from app.workflows.supervisor import SupervisorWorkflow
    from test_agent_v2 import Models, ScriptedModel, Web

    class RewriteRetriever(Retriever):
        def retrieve(self, *args, embedding_query=None, **kwargs):
            return super().retrieve(*args, **kwargs)

    s = control
    s.settings.knowledge_quality_enabled = True
    payload = {"rewrite_method": method, "rewritten_query": "NX-1 2024 保修",
               "step_back_question": "保修适用条件" if method == "step_back" else "",
               "hyde_document": "假设性售后条款，仅供向量匹配" if method == "hyde" else ""}
    workflow, ctx, model, retriever = setup([call("delegate_rag", task="保修"), plan(),
        grade("rewrite", rewritten_query="2024 保修", rewrite_method=method), AIMessage(content=json.dumps(payload)),
        grade(numbers=[2], summary="保修两年[2]"), final("保修两年[2]")],
        settings=s.settings, retriever=RewriteRetriever(["功率100W", "保修两年"]))
    ctx = replace(ctx, user_id=s.uid, session_id=s.sid)
    first = workflow.run(ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert model.position == 6 and len(retriever.calls) == 2
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    empty = ScriptedModel(responses=[])
    repeated = SupervisorWorkflow(s.settings, retriever, Models(empty), Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert repeated.answer.content == first.answer.content and repeated.citations == first.citations
    assert empty.position == 0 and len(retriever.calls) == 2 and repeated.run_metadata["model_attempts"] == 6
