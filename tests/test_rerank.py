import json
from dataclasses import replace
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.api.schemas import KnowledgeCitation
from app.application.evidence import EvidenceRegistry
from app.application.retriever import RetrieverService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, ParentContext, RunContext, SearchHit
from app.infrastructure.rerank import TextReranker
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report


def candidates():
    uid, did = uuid4(), uuid4()
    hits = [SearchHit(Chunk(str(uuid4()), str(uid), str(did), i, f"型号{i} 功率{100+i}W", "spec.md"), .03 - i * .001)
            for i in range(4)]
    return uid, did, hits


def settings(**kwargs):
    return Settings(_env_file=None, rerank_enabled=True, rerank_endpoint="https://rerank.example/api",
                    rerank_api_key="test-only-key", **kwargs)


def ranking():
    return [{"index": 3, "relevance_score": .99}, {"index": 1, "relevance_score": .8}]


def service(hits, reranker=None, expander=None):
    return RetrieverService(SimpleNamespace(embed_query=lambda query: [1.]),
                            SimpleNamespace(search=lambda *args: hits), 2,
                            reranker=reranker, context_expander=expander)


@pytest.mark.parametrize("wrapped", [True, False])
def test_rerank_promotes_candidates_before_parent_expansion_and_keeps_leaf_scores(wrapped):
    uid, did, hits = candidates()
    requests = []

    def respond(request):
        requests.append(request)
        body = {"results": ranking()}
        return httpx.Response(200, json={**({"output": body} if wrapped else body), "usage": {"total_tokens": 123}})

    reranker = TextReranker(settings(), transport=httpx.MockTransport(respond))
    parent = ParentContext(tuple(hit.chunk for hit in hits))
    expanded_ids = []

    def expand(user_id, document_ids, selected):
        expanded_ids.extend(hit.chunk.chunk_id for hit in selected)
        return [replace(hit, parent_context=parent) for hit in selected]

    try:
        result = service(hits, reranker, expand).retrieve(uid, "型号3功率", [did], deadline=monotonic() + 2)
    finally:
        reranker.close()
    assert expanded_ids == [hits[3].chunk.chunk_id, hits[1].chunk.chunk_id]
    assert result[0].score == hits[3].score
    assert result[0].rerank_score == .99
    assert result[0].rerank_trace.total_tokens == 123
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["input"]["documents"] == [hit.chunk.content for hit in hits]
    assert payload["parameters"] == {"top_n": 2, "return_documents": False}
    assert 0 < requests[0].extensions["timeout"]["read"] <= 2
    registry = EvidenceRegistry()
    tool_result = registry.add_knowledge(result)
    citation = KnowledgeCitation.model_validate(registry.validate("grounded", "功率103W[1]")[0])
    assert citation.chunk_id == UUID(hits[3].chunk.chunk_id)
    assert citation.score == hits[3].score and citation.rerank_score == .99
    assert tool_result["rerank"]["status"] == "applied"
    assert registry.rerank_events[0]["candidate_count"] == 4
    assert "number" not in tool_result["contexts"][0]


@pytest.mark.parametrize("status,code", [(401, "RERANK_AUTH_ERROR"), (403, "RERANK_AUTH_ERROR"),
                                         (429, "RERANK_RATE_LIMITED"), (500, "RERANK_UNAVAILABLE"), (302, "RERANK_UNAVAILABLE")])
def test_provider_errors_fall_back_once_without_leaking_body_or_following_redirect(status, code):
    uid, did, hits = candidates()
    respond = MagicMock(return_value=httpx.Response(status, text="test-only-key private data",
                                                    headers={"Location": "https://other.example"}))
    reranker = TextReranker(settings(), transport=httpx.MockTransport(respond))
    try:
        result = service(hits, reranker).retrieve(uid, "query", [did])
    finally:
        reranker.close()
    respond.assert_called_once()
    assert [hit.chunk for hit in result] == [hit.chunk for hit in hits[:2]]
    assert result[0].rerank_score is None
    tool_result = EvidenceRegistry().add_knowledge(result)
    assert tool_result["warnings"] == [code]
    assert tool_result["rerank"]["attempts"] == 1
    assert "test-only-key" not in str(tool_result)


@pytest.mark.parametrize("results", [[], [ranking()[0]], ranking() + [ranking()[0]],
    [{"index": True, "relevance_score": .9}, ranking()[1]],
    [{"index": -1, "relevance_score": .9}, ranking()[1]],
    [{"index": 4, "relevance_score": .9}, ranking()[1]],
    [ranking()[0], ranking()[0]],
    [{"index": 3, "relevance_score": float("nan")}, ranking()[1]],
    [{"index": 3, "relevance_score": 1.1}, ranking()[1]],
    [{"index": 3, "relevance_score": True}, ranking()[1]],
    [{"index": 3}, ranking()[1]], None])
def test_invalid_provider_rankings_preserve_rrf_order(results):
    _, _, hits = candidates()
    # json.dumps permits NaN so the decoder's nonfinite validation is exercised.
    reranker = TextReranker(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=json.dumps({"output": {"results": results}}))))
    try:
        result = reranker.rerank("query", hits, 2)
    finally:
        reranker.close()
    assert [hit.chunk for hit in result] == [hit.chunk for hit in hits[:2]]
    assert result[0].rerank_trace.error_code == "RERANK_INVALID_RESPONSE"


@pytest.mark.parametrize("failure,code", [(httpx.ReadTimeout, "RERANK_TIMEOUT"), (httpx.ConnectError, "RERANK_UNAVAILABLE")])
def test_network_failures_are_recorded(failure, code):
    _, _, hits = candidates()

    def fail(request):
        raise failure("secret provider detail", request=request)

    reranker = TextReranker(settings(), transport=httpx.MockTransport(fail))
    try:
        result = reranker.rerank("query", hits, 2)
    finally:
        reranker.close()
    assert result[0].rerank_trace.error_code == code
    assert "secret provider detail" not in str(result)


def test_expired_deadline_does_not_call_provider_and_disabled_path_has_no_trace():
    uid, did, hits = candidates()
    respond = MagicMock()
    reranker = TextReranker(settings(), transport=httpx.MockTransport(respond))
    try:
        result = service(hits, reranker).retrieve(uid, "query", [did], deadline=monotonic() - 1)
        assert result[0].rerank_trace.error_code == "RERANK_TIMEOUT"
        assert result[0].rerank_trace.attempts == 0
        assert reranker.rerank("q", [], 2) == []
    finally:
        reranker.close()
    respond.assert_not_called()
    assert service(hits).retrieve(uid, "query", [did]) == hits[:2]


def test_all_candidates_are_authorized_before_any_external_request():
    uid, did, hits = candidates()
    hits[3] = replace(hits[3], chunk=replace(hits[3].chunk, user_id=str(uuid4())))
    reranker = MagicMock()
    with pytest.raises(AppError) as error:
        service(hits, reranker).retrieve(uid, "query", [did])
    assert error.value.code == "VECTOR_SCOPE_MISMATCH"
    reranker.rerank.assert_not_called()


def test_bounded_input_does_not_shorten_cited_evidence_or_trust_provider_document():
    uid, did, hits = candidates()
    hits[3] = replace(hits[3], chunk=replace(hits[3].chunk, content="私有文档" * 3000))
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": [{**ranking()[0], "document": {"text": "伪造"}}, ranking()[1]]}})

    reranker = TextReranker(settings(), transport=httpx.MockTransport(respond))
    try:
        result = service(hits, reranker).retrieve(uid, "q" * 3000, [did])
    finally:
        reranker.close()
    assert len(requests[0]["input"]["query"]) == 200
    assert max(map(len, requests[0]["input"]["documents"])) <= 1000
    assert result[0].chunk == hits[3].chunk
    assert result[0].rerank_trace.input_truncated


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"output":null}', b"x" * 65537],
                         ids=["invalid-json", "array", "null-output", "oversized"])
def test_malformed_or_large_response_falls_back(body):
    _, _, hits = candidates()
    reranker = TextReranker(settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    try:
        result = reranker.rerank("q", hits, 2)
    finally:
        reranker.close()
    assert result[0].rerank_trace.error_code == "RERANK_INVALID_RESPONSE"


@pytest.mark.parametrize("endpoint", ["http://rerank.example", "https://user:pass@rerank.example", "https://rerank.example?key=secret",
                                      "https://rerank.example:bad"])
def test_enabled_settings_reject_unsafe_endpoint(endpoint):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_enabled=True, rerank_endpoint=endpoint, rerank_api_key="test")


def test_enabled_settings_require_explicit_key_and_endpoint():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rerank_enabled=True)
    assert not Settings(_env_file=None).rerank_enabled


def test_supervisor_persists_rerank_failure_and_keeps_valid_citations():
    uid, did, hits = candidates()
    reranker = TextReranker(settings(), transport=httpx.MockTransport(lambda request: httpx.Response(429)))
    retriever = service(hits, reranker)
    model = ScriptedModel(responses=[call("delegate_rag", task="参数查询"), call("search_knowledge", query="型号0"), report(), final("功率100W[1]")])
    workflow = SupervisorWorkflow(Settings(_env_file=None), retriever, Models(model), Web())
    context = RunContext(request_id="rerank-test", user_id=uid, session_id=uuid4(), question="型号0的功率？",
                         search_mode="knowledge", allowed_document_ids=(did,), selected_provider="deepseek",
                         public_query=None, web_enabled=False)
    try:
        result = workflow.run(context, [], "")
    finally:
        reranker.close()
    assert result.citations[0]["chunk_id"] == hits[0].chunk.chunk_id
    assert result.run_metadata["warnings"] == ["RERANK_RATE_LIMITED"]
    assert result.run_metadata["rerank_events"][0]["status"] == "fallback"
    assert result.run_metadata["tool_events"][0]["rerank"]["attempts"] == 1


def test_candidates_are_deduplicated_before_rerank_and_trace_is_request_local():
    uid, did, hits = candidates()
    hits.insert(1, replace(hits[0], score=.0299))
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"output": {"results": ranking()}})
        return httpx.Response(503)

    reranker = TextReranker(settings(), transport=httpx.MockTransport(respond))
    try:
        retriever = service(hits, reranker)
        first = retriever.retrieve(uid, "q", [did])
        second = retriever.retrieve(uid, "q", [did])
    finally:
        reranker.close()
    assert len(requests[0]["input"]["documents"]) == 4
    assert first[0].rerank_trace.status == "applied"
    assert second[0].rerank_trace.status == "fallback"
    assert second[0].rerank_score is None
    assert first[0].rerank_trace.error_code is None


def test_parent_expander_cannot_erase_rerank_metadata():
    uid, did, hits = candidates()
    reranker = TextReranker(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": ranking()})))

    def tamper(uid, dids, selected):
        return [replace(hit, rerank_score=None) for hit in selected]

    try:
        with pytest.raises(AppError) as error:
            service(hits, reranker, tamper).retrieve(uid, "q", [did])
    finally:
        reranker.close()
    assert error.value.code == "VECTOR_SCOPE_MISMATCH"
