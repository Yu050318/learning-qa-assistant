"""Exercise real upload/build/verification/publication with a corruptible vector store."""
from dataclasses import asdict, replace
from contextlib import nullcontext
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.api.routes import document_response
from app.application.document_publication import DocumentPublication
from app.core.errors import AppError
from app.infrastructure.persistence.models import DocumentIndex, DocumentSeries
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore
from app.domain.contracts import Chunk
from app.core.config import Settings
from test_document_versions import system, upload, ready


@pytest.fixture
def publication(system, monkeypatch):
    s = system
    s.settings.document_publication_enabled = True
    s.ingestion.publication = DocumentPublication(s.settings, s.vectors)
    def read(uid, did, count, *, verify_model=False):
        assert verify_model
        return [chunk for chunk in s.vectors.chunks if chunk.user_id == str(uid) and chunk.document_id == str(did)]
    monkeypatch.setattr(s.vectors, "read_document_chunks", read, raising=False)
    return s


def test_candidate_is_verified_but_unqueryable_until_atomic_publication(publication):
    s = publication
    first = upload(s)
    second = upload(s, "NX-1 功率200W", first.id)
    assert ready(s) == [first.id]
    with pytest.raises(AppError) as error:
        ready(s, [second.id])
    assert error.value.code == "DOCUMENT_INDEX_UNPUBLISHED"
    view = document_response(second, s.settings.embedding_model)
    assert view["can_query"] is False and view["index_info"]["state"] == "verified"
    s.ingestion.activate_version(s.uid, second.id, first.id)
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, first.id).state == "superseded"
        assert session.get(DocumentIndex, second.id).state == "published"
    assert ready(s) == [second.id] and ready(s, [first.id]) == [first.id]


def test_incomplete_vector_write_never_replaces_old_current(publication, monkeypatch):
    s = publication
    first = upload(s)
    second = upload(s, "candidate with missing vectors", first.id, process=False)
    monkeypatch.setattr(s.vectors, "upsert", lambda chunks, vectors: None)
    s.ingestion.process(s.uid, second.id)
    failed = s.ingestion.get(s.uid, second.id)
    assert failed.status == "failed" and failed.parser_metadata["error_code"] == "INDEX_VERIFICATION_FAILED"
    assert ready(s) == [first.id]
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, second.id).state == "failed"
        assert session.get(DocumentIndex, first.id).state == "published"
    assert {chunk.document_id for chunk in s.vectors.chunks} == {str(first.id)}


def test_post_verification_tampering_blocks_publication_without_switching_pointer(publication):
    s = publication
    first = upload(s)
    second = upload(s, "NX-1 功率200W", first.id)
    s.vectors.chunks = [replace(chunk, content="tampered") if chunk.document_id == str(second.id) else chunk for chunk in s.vectors.chunks]
    with pytest.raises(AppError) as error:
        s.ingestion.activate_version(s.uid, second.id, first.id)
    assert error.value.code == "INDEX_VERIFICATION_FAILED"
    with s.database.sessions() as session:
        assert session.get(DocumentSeries, first.id).current_document_id == first.id
        assert session.get(DocumentIndex, second.id).state == "verified"
    assert ready(s) == [first.id]


def test_changed_build_parameters_fail_before_embedding(publication, monkeypatch):
    s = publication
    first = upload(s)
    second = upload(s, "new candidate", first.id, process=False)
    s.settings.chunk_size += 10
    embed = MagicMock(side_effect=AssertionError("must not call embedding"))
    monkeypatch.setattr(s.embeddings, "embed_documents", embed)
    s.ingestion.process(s.uid, second.id)
    failed = s.ingestion.get(s.uid, second.id)
    assert failed.status == "failed" and failed.parser_metadata["error_code"] == "INDEX_BUILD_CONFIGURATION_CHANGED"
    assert embed.call_count == 0 and ready(s) == [first.id]


def test_same_source_is_deduplicated_per_build_and_can_make_new_build_version(publication):
    s = publication
    first = upload(s)
    with pytest.raises(AppError) as error:
        upload(s, base=first.id)
    assert error.value.code == "DUPLICATE_DOCUMENT"
    s.settings.chunk_size += 10
    second = upload(s, base=first.id)
    assert first.content_hash == second.content_hash and first.build_fingerprint != second.build_fingerprint
    assert second.version_info["version_number"] == 2 and ready(s) == [first.id]


def test_publish_conflict_does_not_publish_losing_candidate(publication):
    s = publication
    first = upload(s)
    second = upload(s, "version 2", first.id)
    third = upload(s, "version 3", first.id)
    s.ingestion.activate_version(s.uid, second.id, first.id)
    with pytest.raises(AppError) as error:
        s.ingestion.activate_version(s.uid, third.id, first.id)
    assert error.value.code == "DOCUMENT_VERSION_CONFLICT"
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, second.id).state == "published"
        assert session.get(DocumentIndex, third.id).state == "verified"
    assert ready(s) == [second.id]


@pytest.mark.parametrize("model", [None, "wrong-model", "text-embedding-v4"])
def test_index_readback_checks_actual_row_model_as_well_as_collection_schema(model):
    uid, did = uuid4(), uuid4()
    leaf = Chunk(str(uuid4()), str(uid), str(did), 0, "verified source", "scope.txt")
    row = asdict(leaf)
    if model is not None:
        row["embedding_model"] = model
    client = MagicMock()
    client.query.side_effect = [[row], []]
    store = MilvusVectorStore(Settings(_env_file=None))
    with patch.object(store, "connection", return_value=nullcontext(client)), patch.object(store, "_validate", return_value={"excel_source"}):
        if model == store.settings.embedding_model:
            assert store.read_document_chunks(uid, did, 1, verify_model=True) == [leaf]
        else:
            with pytest.raises(AppError) as error:
                store.read_document_chunks(uid, did, 1, verify_model=True)
            assert error.value.code == "INDEX_VERIFICATION_FAILED"
    client.upsert.assert_not_called()
    client.delete.assert_not_called()


def test_frozen_scope_survives_publish_and_default_collection_changes_but_not_retirement(publication):
    s = publication
    first = upload(s)
    with s.database.sessions() as session:
        frozen = s.ingestion.publication.freeze(session, {first.id: s.uid})
    second = upload(s, "new current", first.id)
    s.ingestion.activate_version(s.uid, second.id, first.id)
    s.settings.milvus_collection = "future_default"
    with s.database.sessions() as session:
        s.ingestion.publication.validate_targets(session, frozen)
        session.get(DocumentIndex, first.id).state = "retiring"
        session.commit()
    with s.database.sessions() as session, pytest.raises(AppError) as error:
        s.ingestion.publication.validate_targets(session, frozen)
    assert error.value.code == "DOCUMENT_SCOPE_CHANGED"


def test_verified_candidate_can_publish_its_bound_model_after_default_rotation(publication, monkeypatch):
    s = publication
    first = upload(s)
    candidate = upload(s, "candidate old model", first.id)
    original_collection = s.settings.milvus_collection
    original_model = s.settings.embedding_model
    selected = []
    monkeypatch.setattr(s.vectors, "for_collection", lambda name, **kwargs:
        selected.append((name, kwargs)) or s.vectors, raising=False)
    s.settings.milvus_collection = "rotated_collection"
    s.settings.embedding_model = "rotated_model"
    s.settings.embedding_dimension = 128
    current = s.ingestion.activate_version(s.uid, candidate.id, first.id)
    assert current.version_info["is_current"] and document_response(current, s.settings.embedding_model)["can_query"]
    assert selected == [(original_collection, {"embedding_model": original_model, "embedding_dimension": 64})]
    with s.database.sessions() as session:
        assert Repository(session).ready_document_ids(s.uid, None, s.settings.embedding_model, require_index=True) == [candidate.id]


@pytest.mark.parametrize("rotate_before_prepare", [False, True])
def test_prepared_graph_reads_original_index_after_default_collection_and_model_change(publication, monkeypatch, rotate_before_prepare):
    from app.application.chat import ChatService
    from app.application.sessions import SessionService
    from app.application.runs import RunStore
    from app.application.retriever import RetrieverService
    from app.domain.contracts import AgentChatInput
    from app.infrastructure.llms import providers
    from app.workflows.supervisor import SupervisorWorkflow
    from types import SimpleNamespace
    from test_agent_v2 import Models, ScriptedModel, Web, call, final, report
    s = publication
    first = upload(s)
    original_collection = s.settings.milvus_collection
    original_model = s.settings.embedding_model
    seen = []
    def backend(index):
        seen.append(index)
        assert index.collection_name == original_collection and index.embedding_model == original_model
        assert index.embedding_dimension == 64
        return RetrieverService(s.embeddings, s.vectors, 6)
    def frozen_retriever(settings):
        assert settings.embedding_model == ("future_model" if rotate_before_prepare else original_model)
        assert settings.embedding_dimension == (128 if rotate_before_prepare else 64)
        return RetrieverService(SimpleNamespace(), SimpleNamespace(), 6, index_factory=backend)
    retriever = RetrieverService(SimpleNamespace(), SimpleNamespace(), 6, settings_factory=frozen_retriever)
    model = ScriptedModel(responses=[call("delegate_rag", task="功率"), call("search_knowledge", query="NX-1 功率"), report(), final("功率100W[1]")])
    monkeypatch.setattr(providers, "ModelRouter", lambda *args: Models(model))
    sessions = SessionService(s.database)
    conversation = sessions.create(s.uid, "index freeze")
    workflow = SupervisorWorkflow(s.settings, retriever, Models(model), Web())
    chat = ChatService(s.settings, s.database, sessions, workflow)
    store = RunStore(s.settings, s.database)
    if rotate_before_prepare:
        s.settings.milvus_collection = "future_default"
        s.settings.embedding_model = "future_model"
        s.settings.embedding_dimension = 128
        document = s.ingestion.get(s.uid, first.id)
        assert document_response(document, s.settings.embedding_model)["can_query"]
    run, _ = store.create(s.uid, conversation.id, AgentChatInput("功率？", "knowledge"), "frozen-version", uuid4(), queued=True, prepare=chat.prepare_run)
    s.settings.milvus_collection = "future_default"
    s.settings.embedding_model = "future_model"
    s.settings.embedding_dimension = 128
    message = chat.execute_prepared(store, store.claim_next())
    assert message.content == "功率100W[1]" and message.citations[0]["document_id"] == str(first.id)
    assert len(seen) == 1 and seen[0].document_id == first.id
    assert store.read(s.uid, run.id)[0].state == "succeeded"
