from contextlib import nullcontext
from dataclasses import replace, asdict
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

import pytest
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import select, func, delete

from app.application.evidence import EvidenceRegistry, count_tokens
from app.application.hierarchy import HierarchyService, publish_tree
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, ExcelSource, SearchHit
from app.hierarchy_backfill import backfill
from app.infrastructure.loaders.hierarchy import HierarchyBuilder
from app.infrastructure.persistence.models import DocumentRecord, HierarchyNode
from test_document_versions import system
from app.application.retriever import RetrieverService
from app.domain.contracts import RunContext
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def sample_leaves(uid, did, count=8):
    return [Chunk(str(uuid4()), str(uid), str(did), i, f"Parameter{i}: " + "text " * 26, "manual.md") for i in range(count)]


def install(system, leaves=None):
    leaves = leaves or sample_leaves(system.uid, uuid4())
    did = UUID(leaves[0].document_id)
    with system.database.sessions() as session:
        doc = DocumentRecord(id=did, user_id=system.uid, original_name="manual.md", file_type=".md", file_path="unused",
                             content_hash="f" * 64, status="ready", chunk_count=len(leaves), embedding_model=system.settings.embedding_model,
                             parser_metadata={"generation": 1})
        session.add(doc)
        session.flush()
        tree = HierarchyBuilder(system.settings).from_leaves(leaves)
        publish_tree(session, doc, tree)
        session.commit()
    return did, tree


def service(system):
    return HierarchyService(system.settings, system.database)


def test_recursive_three_levels_are_deterministic_and_source_bounded():
    settings = Settings(_env_file=None, chunk_size=100, chunk_overlap=10)
    uid, did = uuid4(), uuid4()
    documents = [Document(page_content=(f"page{page} word. " * 600), metadata={"page_number": page, "section": "same heading"}) for page in (1, 2)]
    builder = HierarchyBuilder(settings)
    with patch("app.infrastructure.loaders.hierarchy.RecursiveCharacterTextSplitter.from_tiktoken_encoder",
               side_effect=lambda **kw: RecursiveCharacterTextSplitter(chunk_size=kw["chunk_size"], chunk_overlap=kw["chunk_overlap"])):
        tree = builder.build(documents, uid, did, "manual.md")
        assert tree == builder.build(documents, uid, did, "manual.md")
    assert tree.fingerprint != HierarchyBuilder(settings.model_copy(update={"chunk_overlap": 11})).fingerprint
    assert {n["level"] for n in tree.nodes} == {1, 2, 3}
    assert [c.chunk_index for c in tree.leaves] == list(range(len(tree.leaves)))
    nodes = {n["node_id"]: n for n in tree.nodes}
    for n in tree.nodes:
        assert len(n["text"]) <= settings.chunk_size * {1: 4, 2: 2, 3: 1}[n["level"]]
        if n["parent_id"]:
            parent = nodes[n["parent_id"]]
            assert parent["level"] == n["level"] - 1
            assert n["text"] in parent["text"]
            assert parent["node_metadata"]["page_number"] == n["node_metadata"]["page_number"]
    assert len([n for n in tree.nodes if n["level"] == 1]) > 2


def test_excel_groups_keep_exact_rows_and_never_cross_sheets():
    uid, did = uuid4(), uuid4()
    leaves = [Chunk(str(uuid4()), str(uid), str(did), i, f"field={i}", "report.xlsx",
                    excel_source=ExcelSource(sheet, i + 2, i + 2, f"sheet:{sheet}:row:{i + 2}"))
              for i, sheet in enumerate(["A"] * 4 + ["B"] * 4)]
    documents = [Document(page_content=c.content, metadata={"excel_source": {"sheet_name": c.excel_source.sheet_name,
                             "row_start": c.excel_source.row_start, "row_end": c.excel_source.row_end,
                             "source_anchor": c.excel_source.source_anchor}}) for c in leaves]
    tree = HierarchyBuilder(Settings(_env_file=None)).build(documents, uid, did, "report.xlsx")
    assert [c.excel_source for c in tree.leaves] == [c.excel_source for c in leaves]
    assert [c.content for c in tree.leaves] == [c.content for c in leaves]
    assert sum(n["level"] == 1 for n in tree.nodes) == 2
    nodes = {n["node_id"]: n for n in tree.nodes}
    assert all(n["node_metadata"]["excel_source"]["sheet_name"] == nodes[n["parent_id"]]["node_metadata"]["excel_source"]["sheet_name"]
               for n in tree.nodes if n["parent_id"])


def test_coverage_selects_root_then_budget_falls_back_to_parent(system):
    # Four leaf children / parent and two parents / root make the coverage thresholds visible.
    system.settings.chunk_size = 100
    leaves = sample_leaves(system.uid, uuid4())
    did, tree = install(system, leaves)
    parents = [n for n in tree.nodes if n["level"] == 2]
    assert len(parents) == 2
    hits = [SearchHit(c, .03) for c in leaves]
    merged = service(system).expand(system.uid, [did], hits, token_budget=10000)
    assert all(h.parent_context.level == 1 for h in merged)
    assert len({h.parent_context.context_id for h in merged}) == 1
    assert merged[0].hierarchy_trace["matched_children"] == 2
    budget = count_tokens(parents[0]["text"])
    smaller = service(system).expand(system.uid, [did], hits[:4], token_budget=budget)
    assert all(h.parent_context.level == 2 for h in smaller)
    none = service(system).expand(system.uid, [did], hits[:1], token_budget=10000)
    assert none[0].parent_context is None
    assert none[0].hierarchy_trace["status"] == "coverage"
    tiny = service(system).expand(system.uid, [did], hits, token_budget=1)
    assert all(h.parent_context is None and h.context_warning == "PARENT_CONTEXT_BUDGET_EXCEEDED" for h in tiny)


def test_context_does_not_create_citations_for_unmatched_leaves(system):
    did, tree = install(system)
    hits = [SearchHit(c, .03) for c in tree.leaves[:5]]
    expanded = service(system).expand(system.uid, [did], hits)
    registry = EvidenceRegistry()
    payload = registry.add_knowledge(expanded)
    assert len(payload["sources"]) == 5 and len(payload["contexts"]) == 1
    assert "Parameter7" in payload["contexts"][0]["content"]
    assert len(registry.entries) == 5 and len(registry.hierarchy_events) == 5
    with pytest.raises(AppError):
        registry.validate("grounded", "invented context citation[6]")


@pytest.mark.parametrize("mutation", ["owner", "text", "parent", "metadata"])
def test_corrupt_tree_is_rejected(system, mutation):
    did, tree = install(system)
    leaf = tree.leaves[0]
    with system.database.sessions() as session:
        node = session.get(HierarchyNode, UUID(leaf.chunk_id))
        if mutation == "owner":
            # Caller scope is rejected even before a database query.
            leaf = replace(leaf, user_id=str(system.other))
        elif mutation == "text":
            node.text = "poison"
        elif mutation == "parent":
            node.parent_id = node.node_id
        else:
            node.node_metadata = {**node.node_metadata, "section": "foreign section"}
        if mutation != "parent":
            session.commit()
        else:
            # FK permits a same-document edge; level validation must reject it.
            session.commit()
    with pytest.raises(AppError) as error:
        service(system).expand(system.uid, [did], [SearchHit(leaf, .03)])
    assert error.value.code in {"HIERARCHY_SCOPE_MISMATCH", "HIERARCHY_INVALID"}


def test_stale_manifest_and_deadline_do_not_widen_context(system):
    did, tree = install(system)
    with system.database.sessions() as session:
        doc = session.get(DocumentRecord, did)
        doc.parser_metadata = {**doc.parser_metadata, "generation": 2}
        session.commit()
    result = service(system).expand(system.uid, [did], [SearchHit(tree.leaves[0], .03)])
    assert result[0].context_warning == "HIERARCHY_STALE" and result[0].parent_context is None
    with pytest.raises(AppError, match="时间预算"):
        service(system).expand(system.uid, [did], [SearchHit(tree.leaves[0], .03)], deadline=0)


def test_ingestion_publishes_only_after_vectors_and_failure_leaves_no_nodes(system):
    system.settings.hierarchy_enabled = True
    upload = system.ingestion.upload(system.uid, "manual.md", "text/markdown", BytesIO(("hello hierarchy. " * 1500).encode()))
    original = system.vectors.upsert

    def before_publish(chunks, vectors):
        with system.database.sessions() as session:
            assert session.scalar(select(func.count()).select_from(HierarchyNode)) == 0
        original(chunks, vectors)

    with patch.object(system.vectors, "upsert", side_effect=before_publish):
        system.ingestion.process(system.uid, upload.id, raise_errors=True)
    doc = system.ingestion.get(system.uid, upload.id)
    assert doc.status == "ready" and doc.parser_metadata["hierarchy"]["leaf_count"] == doc.chunk_count
    with system.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HierarchyNode)) > doc.chunk_count
    failed = system.ingestion.upload(system.uid, "failure.md", "text/markdown", BytesIO(b"different source text"))
    with patch.object(system.vectors, "upsert", side_effect=RuntimeError("offline failure")):
        system.ingestion.process(system.uid, failed.id)
    assert system.ingestion.get(system.uid, failed.id).status == "failed"
    with system.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(HierarchyNode).where(HierarchyNode.document_id == failed.id)) == 0


def test_backfill_preserves_leaf_ids_and_never_invokes_embedding_or_vector_writes(system):
    system.settings.hierarchy_enabled = True
    did, tree = install(system)
    system.database.document_lock = lambda did: nullcontext(lambda: None)
    vectors = SimpleNamespace(read_document_chunks=Mock(return_value=tree.leaves))
    before = list(tree.leaves)
    assert backfill(system.settings, system.database, vectors, system.uid, did) == len(tree.nodes)
    assert tree.leaves == before
    vectors.read_document_chunks.assert_called_once_with(system.uid, did, len(before))
    with system.database.sessions() as session:
        ids = set(session.scalars(select(HierarchyNode.node_id).where(HierarchyNode.document_id == did, HierarchyNode.level == 3)))
    assert ids == {UUID(c.chunk_id) for c in before}
    with pytest.raises(AppError):
        backfill(system.settings, system.database, vectors, system.other, did)


def test_backfill_rechecks_document_after_vector_read(system):
    system.settings.hierarchy_enabled = True
    did, tree = install(system)
    system.database.document_lock = lambda did: nullcontext(lambda: None)
    def read(*args):
        with system.database.sessions() as session:
            document = session.get(DocumentRecord, did)
            document.parser_metadata = {**document.parser_metadata, 'generation': 99}
            session.commit()
        return tree.leaves
    with pytest.raises(AppError) as error:
        backfill(system.settings, system.database, SimpleNamespace(read_document_chunks=read), system.uid, did)
    assert error.value.code == 'HIERARCHY_STALE'


def test_legacy_fallback_does_not_rebuild_or_write(system):
    did, tree = install(system)
    with system.database.sessions() as session:
        doc = session.get(DocumentRecord, did)
        doc.parser_metadata = {"generation": 1}
        session.commit()
    hits = [SearchHit(tree.leaves[0], .03)]
    legacy = Mock(return_value=hits)
    result = HierarchyService(system.settings, system.database, legacy_expander=legacy).expand(system.uid, [did], hits)
    legacy.assert_called_once_with(system.uid, [did], hits, deadline=None)
    assert result[0].hierarchy_trace["status"] == "legacy"


def test_missing_child_cannot_inflate_coverage(system):
    did, tree = install(system)
    with system.database.sessions() as session:
        session.execute(delete(HierarchyNode).where(HierarchyNode.node_id == UUID(tree.leaves[-1].chunk_id)))
        session.commit()
    with pytest.raises(AppError) as error:
        service(system).expand(system.uid, [did], [SearchHit(c, .03) for c in tree.leaves[:5]])
    assert error.value.code == "HIERARCHY_SCOPE_MISMATCH"


def test_supervisor_receives_context_but_cites_real_leaves_and_records_merge(system):
    did, tree = install(system)
    selected = tree.leaves[:5]
    vectors = SimpleNamespace(search=lambda *args: [SearchHit(c, .03) for c in selected])
    retriever = RetrieverService(system.embeddings, vectors, 6, hierarchy_expander=service(system).expand)
    model = ScriptedModel(responses=[call("delegate_rag", task="参数查询"), call("search_knowledge", query="Parameter0"), report(), final("查到参数[1]")])
    workflow = SupervisorWorkflow(system.settings, retriever, Models(model), Web())
    context = RunContext(request_id="hierarchy-test", user_id=system.uid, session_id=uuid4(), question="Parameter0 是什么？",
                         search_mode="knowledge", allowed_document_ids=(did,), selected_provider="deepseek", public_query=None, web_enabled=False)
    result = workflow.run(context, [], "")
    assert result.citations[0]["chunk_id"] == selected[0].chunk_id
    assert result.run_metadata["parent_context_count"] == 1
    assert result.run_metadata["hierarchy_events"][0]["selected_level"] == 1
    assert "Parameter7" in str(model.seen_calls[-1])
    assert "Parameter7" not in result.citations[0]["excerpt"]


@pytest.mark.parametrize("code", ["HIERARCHY_UNAVAILABLE", "HIERARCHY_TIMEOUT"])
def test_expansion_outage_keeps_ranked_leaf_evidence(system, code):
    did, tree = install(system)
    hits = [SearchHit(tree.leaves[0], .03, rerank_score=.7)]
    retriever = RetrieverService(system.embeddings, SimpleNamespace(search=lambda *args: hits), 6,
                               hierarchy_expander=Mock(side_effect=AppError(code, "fixture")))
    result = retriever.retrieve(system.uid, "Parameter0", [did], deadline=999999999)
    assert result[0].chunk == hits[0].chunk and result[0].rerank_score == .7
    assert result[0].context_warning == code


@pytest.mark.parametrize("corruption", [None, "owner", "extra"])
def test_backfill_vector_reads_are_batched_scoped_and_exact(corruption):
    uid, did = uuid4(), uuid4()
    leaves = sample_leaves(uid, did, 501)
    rows = [asdict(c) for c in leaves]
    if corruption == "owner":
        rows[0]["user_id"] = str(uuid4())
    client = Mock()
    client.query.side_effect = [rows[:500], rows[500:], [{"chunk_id": str(uuid4())}] if corruption == "extra" else []]
    store = MilvusVectorStore(Settings(_env_file=None))
    with patch.object(store, "connection", return_value=nullcontext(client)), patch.object(store, "_validate", return_value={"excel_source"}):
        if corruption:
            with pytest.raises(AppError):
                store.read_document_chunks(uid, did, len(leaves))
        else:
            assert store.read_document_chunks(uid, did, len(leaves)) == leaves
            assert client.query.call_count == 3
            assert all(str(uid) in c.kwargs["filter"] and str(did) in c.kwargs["filter"] for c in client.query.call_args_list)
            assert [c.kwargs["limit"] for c in client.query.call_args_list] == [501, 2, 1]
    client.upsert.assert_not_called()
    client.delete.assert_not_called()
