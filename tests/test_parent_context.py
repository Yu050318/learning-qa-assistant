from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.api.schemas import KnowledgeCitation
from app.application.evidence import EvidenceRegistry
from app.application.retriever import RetrieverService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, ExcelSource, ParentContext, RunContext, SearchHit
from app.infrastructure.vectorstores.milvus import MilvusVectorStore
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report


def leaves(*, excel=False):
    user_id, doc_id = uuid4(), uuid4()
    chunks = [Chunk(str(uuid4()), str(user_id), str(doc_id), index, f"型号 NX-00{index}；功率：{100 + index} W", "specs.xlsx" if excel else "manual.md",
                    excel_source=ExcelSource("Specs", index + 2, index + 2, f"sheet:Specs:row:{index + 2}") if excel else None)
              for index in range(4)]
    return user_id, doc_id, chunks


def row(chunk):
    result = asdict(chunk)
    result.update(page_number=chunk.page_number or 0, section=chunk.section or "")
    return result


def with_store(chunks, hits, *, fields=None, timeout=10):
    settings = Settings(_env_file=None, milvus_timeout=timeout)
    store = MilvusVectorStore(settings)
    client = MagicMock()
    client.query.return_value = [row(chunk) for chunk in reversed(chunks)]
    connection = MagicMock()
    connection.__enter__.return_value = client
    with patch.object(store, "connection", return_value=connection), patch.object(store, "_validate", return_value=fields or set()):
        result = store.expand_context(UUID(hits[0].chunk.user_id), [UUID(hits[0].chunk.document_id)], hits)
    return result, client


def test_bounded_parent_reads_are_scoped_cached_and_keep_leaf_rankings():
    uid, did, chunks = leaves()
    hits = [SearchHit(chunks[2], .03), SearchHit(chunks[0], .02)]
    expanded, client = with_store(chunks, hits)
    client.query.assert_called_once()
    query = client.query.call_args.kwargs
    assert f'user_id == "{uid}"' in query["filter"]
    assert f'"{did}"' in query["filter"]
    assert "chunk_index >= 0 and chunk_index < 4" in query["filter"]
    assert query["limit"] == 5
    assert 0 < query["timeout"] <= 10
    assert [(hit.chunk, hit.score) for hit in expanded] == [(hit.chunk, hit.score) for hit in hits]
    parent = expanded[0].parent_context
    assert parent.children == tuple(chunks)
    assert parent.context_id == expanded[1].parent_context.context_id
    assert "NX-003" in parent.content


@pytest.mark.parametrize("boundary,value", [("page_number", 2), ("section", "another section"), ("source_name", "other.md")])
def test_parent_does_not_cross_page_section_or_filename(boundary, value):
    _, _, chunks = leaves()
    chunks[3] = replace(chunks[3], **{boundary: value})
    expanded, _ = with_store(chunks, [SearchHit(chunks[0], .03)])
    assert expanded[0].parent_context.children == tuple(chunks[:3])


def test_excel_parents_cannot_cross_sheets_and_do_not_expand_cited_rows():
    _, _, chunks = leaves(excel=True)
    chunks[3] = replace(chunks[3], excel_source=ExcelSource("Other", 5, 5, "sheet:Other:row:5"))
    hits, _ = with_store(chunks, [SearchHit(chunks[1], .03)], fields={"excel_source"})
    assert len(hits[0].parent_context.children) == 3
    registry = EvidenceRegistry()
    result = registry.add_knowledge(hits)
    assert result["contexts"][0]["type"] == "context_only"
    assert "number" not in result["contexts"][0]
    assert result["contexts"][0]["for_numbers"] == [1]
    assert len(registry.entries) == 1
    citation = KnowledgeCitation.model_validate(registry.validate("grounded", "命中行的参数[1]")[0])
    assert citation.excel_source.row_start == citation.excel_source.row_end == 3
    assert citation.chunk_id == UUID(chunks[1].chunk_id)
    assert "NX-001" in citation.excerpt and "NX-002" not in citation.excerpt


@pytest.mark.parametrize("change", ["user", "document", "outside_group", "duplicate_position"])
def test_outside_scope_or_duplicate_context_children_fail_closed(change):
    _, _, chunks = leaves()
    hit = SearchHit(chunks[0], .03)
    if change == "user":
        chunks[3] = replace(chunks[3], user_id=str(uuid4()))
    elif change == "document":
        chunks[3] = replace(chunks[3], document_id=str(uuid4()))
    elif change == "outside_group":
        chunks[3] = replace(chunks[3], chunk_index=9)
    else:
        chunks[3] = replace(chunks[3], chunk_index=2)
    with pytest.raises(AppError) as error:
        with_store(chunks, [hit])
    assert error.value.code in {"VECTOR_SCOPE_MISMATCH", "VECTOR_METADATA_INVALID"}


def test_stale_leaf_content_gets_explicit_warning_instead_of_wrong_parent():
    _, _, chunks = leaves()
    hit = SearchHit(chunks[0], .03)
    chunks[0] = replace(chunks[0], content="changed after initial search")
    result, _ = with_store(chunks, [hit])
    assert result[0].chunk == hit.chunk
    assert result[0].parent_context is None
    assert result[0].context_warning == "PARENT_CONTEXT_STALE"


def test_singleton_and_oversized_parent_keep_only_leaf():
    _, _, chunks = leaves()
    result, _ = with_store(chunks[:1], [SearchHit(chunks[0], .03)])
    assert result[0].parent_context is None
    chunks[1] = replace(chunks[1], content="x" * 12000)
    result, _ = with_store(chunks, [SearchHit(chunks[0], .03)])
    assert result[0].context_warning == "PARENT_CONTEXT_TOO_LARGE"


@pytest.mark.parametrize("code", ["MILVUS_UNAVAILABLE", "PARENT_CONTEXT_TIMEOUT"])
def test_parent_failure_preserves_healthy_leaf_with_visible_warning(code):
    uid, did, chunks = leaves()
    hits = [SearchHit(chunks[0], .03)]

    def expand(*args):
        raise AppError(code, "unavailable", 503)

    service = RetrieverService(SimpleNamespace(embed_query=lambda query: [1.]), SimpleNamespace(search=lambda *args: hits), 6, context_expander=expand)
    result = service.retrieve(uid, "query", [did])
    assert result[0].chunk == hits[0].chunk
    assert result[0].context_warning == code
    assert EvidenceRegistry().add_knowledge(result)["warnings"] == [code]


def test_parent_queries_share_a_deadline():
    _, _, chunks = leaves()
    with patch("app.infrastructure.vectorstores.milvus.monotonic", side_effect=[0, 11]):
        with pytest.raises(AppError) as error:
            with_store(chunks, [SearchHit(chunks[0], .03)])
    assert error.value.code == "PARENT_CONTEXT_TIMEOUT"


def test_context_query_cache_is_local_to_each_expansion():
    uid, did, chunks = leaves()
    store = MilvusVectorStore(Settings(_env_file=None))
    client = MagicMock()
    client.query.return_value = [row(chunk) for chunk in chunks]
    connection = MagicMock()
    connection.__enter__.return_value = client
    with patch.object(store, "connection", return_value=connection), patch.object(store, "_validate", return_value=set()):
        first = store.expand_context(uid, [did], [SearchHit(chunks[0], .03)])
        changed = replace(chunks[3], content="new sibling content")
        client.query.return_value = [row(chunk) for chunk in [*chunks[:3], changed]]
        second = store.expand_context(uid, [did], [SearchHit(chunks[0], .03)])
    assert client.query.call_count == 2
    assert first[0].parent_context.context_id != second[0].parent_context.context_id


def test_invalid_context_scope_does_not_degrade_to_successful_leaf_result():
    uid, did, chunks = leaves()

    def expand(*args):
        raise AppError("VECTOR_SCOPE_MISMATCH", "forbidden", 503)

    service = RetrieverService(SimpleNamespace(embed_query=lambda query: [1.]),
                               SimpleNamespace(search=lambda *args: [SearchHit(chunks[0], .03)]), 6, context_expander=expand)
    with pytest.raises(AppError) as error:
        service.retrieve(uid, "query", [did])
    assert error.value.code == "VECTOR_SCOPE_MISMATCH"


def test_no_expander_keeps_original_retrieval_behavior():
    uid, did, chunks = leaves()
    hits = [SearchHit(chunks[1], .03)]
    service = RetrieverService(SimpleNamespace(embed_query=lambda query: [1.]), SimpleNamespace(search=lambda *args: hits), 6)
    assert service.retrieve(uid, "query", [did]) == hits


def test_legacy_excel_with_unknown_sheet_is_not_grouped():
    _, _, chunks = leaves()
    chunks = [replace(chunk, source_name="old.xlsx") for chunk in chunks]
    result, client = with_store(chunks, [SearchHit(chunks[0], .03)])
    assert result[0].parent_context is None
    assert result[0].context_warning == "PARENT_CONTEXT_BOUNDARY_UNKNOWN"
    client.query.assert_not_called()


@pytest.mark.parametrize("change", ["replace_leaf", "replace_score", "add_hit", "unrelated_parent"])
def test_expander_cannot_change_retrieval_evidence(change):
    uid, did, chunks = leaves()
    hit = SearchHit(chunks[0], .03)
    changed = {
        "replace_leaf": [replace(hit, chunk=chunks[1])],
        "replace_score": [replace(hit, score=.99)],
        "add_hit": [hit, SearchHit(chunks[1], .01)],
        "unrelated_parent": [replace(hit, parent_context=ParentContext(tuple(chunks[1:3])))],
    }[change]
    service = RetrieverService(SimpleNamespace(embed_query=lambda query: [1.]), SimpleNamespace(search=lambda *args: [hit]), 6,
                               context_expander=lambda *args: changed)
    with pytest.raises(AppError):
        service.retrieve(uid, "query", [did])


def test_context_deduplicates_across_searches_and_never_creates_citation_numbers():
    _, _, chunks = leaves()
    parent = ParentContext(tuple(chunks))
    registry = EvidenceRegistry()
    first = registry.add_knowledge([SearchHit(chunks[0], .03, parent)])
    used = registry.used_tokens
    registry.add_knowledge([SearchHit(chunks[0], .03, parent)])
    assert registry.used_tokens == used
    second = registry.add_knowledge([SearchHit(chunks[2], .02, parent)])
    assert first["contexts"][0]["context_id"] == second["contexts"][0]["context_id"]
    assert second["contexts"][0]["for_numbers"] == [1, 2]
    assert [item["number"] for item in registry.entries] == [1, 2]
    with pytest.raises(AppError):
        registry.validate("grounded", "不能引用没有命中的第三行[3]")


def test_parent_budget_cannot_crowd_out_all_leaf_evidence():
    _, _, chunks = leaves()
    chunks[1] = replace(chunks[1], content="x" * 2000)
    registry = EvidenceRegistry(token_budget=1000)
    result = registry.add_knowledge([SearchHit(chunks[0], .03, ParentContext(tuple(chunks)))])
    assert result["contexts"] == []
    assert result["sources"][0]["number"] == 1
    assert result["warnings"] == ["PARENT_CONTEXT_BUDGET_EXCEEDED"]


def test_registry_rejects_unrelated_parent_before_registering_any_source():
    _, _, chunks = leaves()
    registry = EvidenceRegistry()
    with pytest.raises(AppError):
        registry.add_knowledge([SearchHit(chunks[0], .03, ParentContext(tuple(chunks[1:3])))])
    assert registry.entries == []


def test_supervisor_receives_unnumbered_context_and_final_citation_stays_on_leaf():
    uid, did, chunks = leaves(excel=True)
    parent = ParentContext(tuple(chunks))
    model = ScriptedModel(responses=[call("delegate_rag", task="参数查询"), call("search_knowledge", query="NX-001"), report(), final("功率 101W[1]")])
    retriever = SimpleNamespace(retrieve=lambda *args, **kwargs: [SearchHit(chunks[1], .03, parent)])
    workflow = SupervisorWorkflow(Settings(_env_file=None), retriever, Models(model), Web())
    context = RunContext(request_id="parent-test", user_id=uid, session_id=uuid4(), question="NX-001 的功率？",
                         search_mode="knowledge", allowed_document_ids=(did,), selected_provider="deepseek",
                         public_query=None, web_enabled=False)
    result = workflow.run(context, [], "")
    assert result.citations[0]["excel_source"]["row_start"] == 3
    assert result.run_metadata["parent_context_count"] == 1
    assert result.run_metadata["tool_events"][0]["parent_context_count"] == 1
    assert "context_only" in str(model.seen_calls[-1])
    assert "NX-003" in str(model.seen_calls[-1])
