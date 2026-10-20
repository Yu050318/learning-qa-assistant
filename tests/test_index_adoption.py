"""Legacy adoption checks complete source leaves and only persists their binding."""
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.index_adoption import adopt
from app.infrastructure.persistence.models import ChatRun, ChatSession, DocumentIndex, DocumentSeries, DocumentVersion, HierarchyNode, IngestionJob, WorkerHeartbeat, utcnow
from test_document_versions import system
from test_ingestion_worker import durable, upload


@pytest.fixture
def legacy(durable, monkeypatch):
    s = durable
    s.doc = upload(s, "original complete source. " * 100)
    assert s.worker.run_once()
    s.original_chunks = list(s.vectors.chunks)
    monkeypatch.setattr(s.vectors, "read_document_chunks", Mock(side_effect=lambda uid, did, count, **kwargs:
        [chunk for chunk in s.vectors.chunks if chunk.user_id == str(uid) and chunk.document_id == str(did)]), raising=False)
    monkeypatch.setattr(s.embeddings, "embed_documents", Mock(side_effect=AssertionError("must not embed during adoption")))
    monkeypatch.setattr(s.vectors, "upsert", Mock(side_effect=AssertionError("must not write vectors during adoption")))
    monkeypatch.setattr(s.vectors, "delete_document", Mock(side_effect=AssertionError("must not delete vectors during adoption")))
    return s


def apply(s, reviewed=None):
    reviewed = reviewed or adopt(s.ingestion, s.uid, s.doc.id)
    return adopt(s.ingestion, s.uid, s.doc.id, apply=True, maintenance=True, expected_leaf_sha256=reviewed["leaf_sha256"])


def test_dry_run_application_and_repeat_preserve_source_ids_and_current_pointer(legacy):
    s = legacy
    source = s.ingestion.file_path(s.doc.file_path).read_bytes()
    before = s.ingestion.get(s.uid, s.doc.id).parser_metadata
    preview = adopt(s.ingestion, s.uid, s.doc.id)
    assert preview["status"] == "verified" and not preview["applied"]
    assert preview["leaf_count"] == len(s.original_chunks) > 1
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, s.doc.id) is None
    assert s.ingestion.get(s.uid, s.doc.id).parser_metadata == before
    applied = apply(s, preview)
    assert applied["applied"] and applied["state"] == "published"
    with s.database.sessions() as session:
        index = session.get(DocumentIndex, s.doc.id)
        assert index.leaf_sha256 == preview["leaf_sha256"] and index.build_fingerprint == "legacy"
        assert session.get(DocumentSeries, s.doc.id).current_document_id == s.doc.id
    after = s.ingestion.get(s.uid, s.doc.id).parser_metadata
    assert apply(s, preview)["status"] == "already_bound"
    assert s.ingestion.get(s.uid, s.doc.id).parser_metadata == after
    assert s.vectors.chunks == s.original_chunks
    assert s.ingestion.file_path(s.doc.file_path).read_bytes() == source
    assert s.vectors.read_document_chunks.call_args.kwargs == {"verify_model": True}


def test_full_content_is_verified_beyond_the_first_matching_fragment(legacy):
    s = legacy
    s.vectors.chunks[1] = replace(s.vectors.chunks[1], content="changed later source fragment")
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_CONTENT_MISMATCH"
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, s.doc.id) is None


def test_original_random_leaf_ids_are_preserved_rather_than_replaced(legacy):
    s = legacy
    s.vectors.chunks = [replace(chunk, chunk_id=str(uuid4())) for chunk in s.vectors.chunks]
    original = list(s.vectors.chunks)
    assert apply(s)["applied"]
    assert s.vectors.chunks == original


def test_original_chunking_parameters_must_match_the_entire_index(legacy):
    s = legacy
    s.settings.chunk_size += 100
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_CONTENT_MISMATCH"


def test_source_bytes_are_verified_even_when_the_leaf_count_matches(legacy):
    s = legacy
    s.ingestion.file_path(s.doc.file_path).write_text("changed original source", encoding="utf-8")
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "DOCUMENT_SOURCE_CHANGED"
    s.vectors.read_document_chunks.assert_not_called()


def test_mineru_uses_complete_hash_matched_cache_without_a_remote_parser(legacy):
    s = legacy
    loaded = s.ingestion.loader.load(s.ingestion.file_path(s.doc.file_path), s.doc.file_type)
    with s.database.sessions() as session:
        document = session.get(type(s.doc), s.doc.id)
        document.parser_metadata = {**document.parser_metadata, "provider": "mineru", "document_type": "pdf"}
        session.commit()
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_EVIDENCE_MISSING"
    s.ingestion._save_cache(s.uid, s.doc.id, s.doc.content_hash, "pdf", loaded)
    result = apply(s)
    assert result["evidence"] == "normalized-cache" and s.ingestion.mineru is None


def test_sparse_or_wrong_source_cache_cannot_bind_a_complete_index(legacy):
    s = legacy
    from langchain_core.documents import Document
    with s.database.sessions() as session:
        document = session.get(type(s.doc), s.doc.id)
        document.parser_metadata = {**document.parser_metadata, "provider": "mineru", "document_type": "pdf"}
        session.commit()
    s.ingestion._save_cache(s.uid, s.doc.id, s.doc.content_hash, "pdf", [Document(page_content=s.original_chunks[0].content)])
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_CONTENT_MISMATCH"
    s.ingestion._save_cache(s.uid, s.doc.id, "f" * 64, "pdf", [Document(page_content="wrong hash")])
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_EVIDENCE_MISSING"


def test_backfilled_hierarchy_is_verified_without_resplitting_it_as_recursive(legacy):
    from app.hierarchy_backfill import backfill
    s = legacy
    s.settings.hierarchy_enabled = True
    backfill(s.settings, s.database, s.vectors, s.uid, s.doc.id)
    result = apply(s)
    assert result["chunking_mode"] == "backfilled-leaves"
    with s.database.sessions() as session:
        assert session.scalars(select(HierarchyNode).where(HierarchyNode.document_id == s.doc.id)).all()


def test_corrupt_hierarchy_is_not_frozen_even_if_vectors_match_the_source(legacy):
    from app.hierarchy_backfill import backfill
    s = legacy
    s.settings.hierarchy_enabled = True
    backfill(s.settings, s.database, s.vectors, s.uid, s.doc.id)
    with s.database.sessions() as session:
        node = session.scalar(select(HierarchyNode).where(HierarchyNode.document_id == s.doc.id, HierarchyNode.level == 3))
        node.text = "corrupt hierarchy"
        session.commit()
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_EVIDENCE_INVALID"


def test_recursive_legacy_tree_uses_original_recursive_leaf_boundaries(durable, monkeypatch):
    s = durable
    s.settings.hierarchy_enabled = True
    s.settings.chunk_size = 100
    s.settings.chunk_overlap = 10
    doc = upload(s, "recursive sections and long leaf boundaries. " * 120)
    assert s.worker.run_once()
    monkeypatch.setattr(s.vectors, "read_document_chunks", lambda *args, **kwargs: list(s.vectors.chunks), raising=False)
    preview = adopt(s.ingestion, s.uid, doc.id)
    assert preview["chunking_mode"] == "recursive"
    result = adopt(s.ingestion, s.uid, doc.id, apply=True, maintenance=True, expected_leaf_sha256=preview["leaf_sha256"])
    assert result["applied"]


def test_wrong_owner_is_rejected_before_any_vector_read(legacy):
    s = legacy
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.other, s.doc.id)
    assert error.value.code == "NOT_FOUND"
    s.vectors.read_document_chunks.assert_not_called()


def test_adoption_target_must_satisfy_the_same_frozen_index_contract(legacy):
    s = legacy
    s.settings.milvus_collection = "invalid-collection"
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id)
    assert error.value.code == "INDEX_ADOPTION_CONFIGURATION_MISMATCH"


@pytest.mark.parametrize("blocker", ["run", "worker", "pending_job"])
def test_apply_requires_quiescent_runtime_and_never_rewrites_old_checkpoints(legacy, blocker):
    s = legacy
    preview = adopt(s.ingestion, s.uid, s.doc.id)
    with s.database.sessions() as session:
        if blocker == "run":
            chat = ChatSession(id=uuid4(), user_id=s.uid, title="old active run")
            session.add(chat)
            session.flush()
            session.add(ChatRun(id=uuid4(), user_id=s.uid, session_id=chat.id, client_key=uuid4(), fingerprint="a" * 64,
                state="waiting", request_id="adoption-test", execution_snapshot={"legacy": True}, expires_at=utcnow() + timedelta(hours=1)))
        elif blocker == "worker":
            session.add(WorkerHeartbeat(id=uuid4(), kind="ingestion", configuration_hash="a" * 64, seen_at=utcnow()))
        else:
            session.get(IngestionJob, s.doc.id).state = "queued"
        session.commit()
    with pytest.raises(AppError) as error:
        apply(s, preview)
    assert error.value.code == ("RESOURCE_BUSY" if blocker == "pending_job" else "INDEX_ADOPTION_MAINTENANCE_REQUIRED")
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, s.doc.id) is None
        if blocker == "run":
            assert session.scalar(select(ChatRun.execution_snapshot)) == {"legacy": True}


def test_apply_requires_explicit_review_and_the_unchanged_readback_digest(legacy):
    s = legacy
    for options in ({"apply": True}, {"apply": True, "maintenance": True}):
        with pytest.raises(AppError) as error:
            adopt(s.ingestion, s.uid, s.doc.id, **options)
        assert error.value.code == "INDEX_ADOPTION_REVIEW_REQUIRED"
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.doc.id, apply=True, maintenance=True, expected_leaf_sha256="f" * 64)
    assert error.value.code == "INDEX_ADOPTION_CONFLICT"


def test_adoption_preserves_existing_history_and_never_selects_a_new_current(durable, monkeypatch):
    s = durable
    first = upload(s)
    assert s.worker.run_once()
    second = s.ingestion.upload(s.uid, "second.md", "text/markdown", BytesIO(b"second immutable source"), base_document_id=first.id)
    assert s.worker.run_once()
    monkeypatch.setattr(s.vectors, "read_document_chunks", lambda uid, did, count, **kwargs:
        [chunk for chunk in s.vectors.chunks if chunk.document_id == str(did)], raising=False)
    preview = adopt(s.ingestion, s.uid, second.id)
    result = adopt(s.ingestion, s.uid, second.id, apply=True, maintenance=True, expected_leaf_sha256=preview["leaf_sha256"])
    assert result["state"] == "superseded"
    with s.database.sessions() as session:
        assert session.get(DocumentSeries, first.id).current_document_id == first.id
        assert session.get(DocumentVersion, second.id).version_number == 2


def test_unversioned_legacy_binding_is_not_written_until_apply(durable, monkeypatch):
    s = durable
    s.settings.document_versions_enabled = False
    doc = upload(s)
    assert s.worker.run_once()
    s.settings.document_versions_enabled = True
    monkeypatch.setattr(s.vectors, "read_document_chunks", lambda *args, **kwargs: list(s.vectors.chunks), raising=False)
    preview = adopt(s.ingestion, s.uid, doc.id)
    with s.database.sessions() as session:
        assert session.get(DocumentVersion, doc.id) is None
    result = adopt(s.ingestion, s.uid, doc.id, apply=True, maintenance=True, expected_leaf_sha256=preview["leaf_sha256"])
    assert result["applied"] and result["state"] == "published"
    with s.database.sessions() as session:
        assert session.get(DocumentVersion, doc.id).version_number == 1


def test_failed_unbound_legacy_upload_cannot_be_retried_as_a_new_frozen_build(legacy):
    from app.application.document_publication import DocumentPublication
    s = legacy
    s.settings.document_publication_enabled = True
    s.ingestion.publication = DocumentPublication(s.settings, s.vectors)
    with s.database.sessions() as session:
        doc = session.get(type(s.doc), s.doc.id)
        doc.status = "failed"
        job = session.get(IngestionJob, s.doc.id)
        job.state = "failed"
        old_task = job.task_id
        session.commit()
    assert not s.ingestion.tasks.detail(s.uid, old_task)["can_retry"]
    with pytest.raises(AppError) as failure:
        s.ingestion.retry(s.uid, s.doc.id)
    assert failure.value.code == "DOCUMENT_INDEX_NOT_ADOPTED"
    with s.database.sessions() as session:
        assert session.get(type(s.doc), s.doc.id).status == "failed"
        assert session.get(IngestionJob, s.doc.id).task_id == old_task
