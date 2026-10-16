from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.api.schemas import V2MessageResponse
from app.application.chat import ChatService
from app.application.ingestion import IngestionService
from app.application.retriever import RetrieverService
from app.application.sessions import SessionService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, SearchHit
from app.infrastructure.loaders.local import LocalDocumentLoader
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Base, DocumentRecord, DocumentSeries, DocumentVersion, User
from app.infrastructure.persistence.repository import Repository
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report


@pytest.fixture
def system(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    connection = engine.connect()
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS rag")
    Base.metadata.create_all(connection)
    database = SimpleNamespace(sessions=sessionmaker(bind=connection, expire_on_commit=False))
    with database.sessions() as session:
        user, other = User(username="version-owner", password_hash="fixture"), User(username="version-other", password_hash="fixture")
        session.add_all([user, other]);session.commit()
    settings = Settings(_env_file=None, document_versions_enabled=True, upload_dir=tmp_path / "uploads",
                        parsed_dir=tmp_path / "parsed", embedding_dimension=64)

    class Vectors:
        chunks = []

        def delete_document(self, uid, did):
            self.chunks = [chunk for chunk in self.chunks if chunk.user_id != str(uid) or chunk.document_id != str(did)]

        def upsert(self, chunks, vectors):
            self.chunks.extend(chunks)

        def search(self, uid, ids, *args):
            return [SearchHit(chunk, .03) for chunk in self.chunks if chunk.user_id == str(uid) and chunk.document_id in {str(did) for did in ids}]

    vectors = Vectors()
    embeddings = SimpleNamespace(embed_documents=lambda values: [[0.] * 64 for value in values], embed_query=lambda value: [0.] * 64)
    ingestion = IngestionService(settings, database, LocalDocumentLoader(settings), None, embeddings, vectors)
    value = SimpleNamespace(settings=settings, database=database, ingestion=ingestion, uid=user.id, other=other.id, vectors=vectors, embeddings=embeddings)
    try:
        # This stage exercises version identity/ingestion, not a network tokenizer download.
        with patch("app.infrastructure.loaders.local.RecursiveCharacterTextSplitter.from_tiktoken_encoder",
                   side_effect=lambda **kwargs: RecursiveCharacterTextSplitter(chunk_size=kwargs["chunk_size"], chunk_overlap=kwargs["chunk_overlap"])):
            yield value
    finally:
        ingestion.close();connection.close();engine.dispose()


def upload(system, content="NX-1 功率100W", base=None, process=True):
    document = system.ingestion.upload(system.uid, "manual.md", "text/markdown", BytesIO(content.encode()), base_document_id=base)
    if process:
        system.ingestion.process(system.uid, document.id)
    result = system.ingestion.get(system.uid, document.id)
    if process:
        assert result.status == "ready", (result.error_message, result.parser_metadata)
    return result


def ready(system, requested=None):
    with system.database.sessions() as session:
        return Repository(session).ready_document_ids(system.uid, requested, system.settings.embedding_model, current_only=True)


def test_new_version_keeps_old_bytes_vectors_and_default_scope_until_activated(system):
    first = upload(system)
    second = upload(system, "NX-1 功率200W", first.id)
    assert first.id != second.id and first.file_path != second.file_path
    assert system.ingestion.file_path(first.file_path).read_text(encoding="utf-8") == "NX-1 功率100W"
    assert first.version_info["version_number"] == 1
    assert second.version_info["version_number"] == 2 and not second.version_info["is_current"]
    assert ready(system) == [first.id]
    assert ready(system, [second.id]) == [second.id]
    system.ingestion.activate_version(system.uid, second.id, first.id)
    assert ready(system) == [second.id]
    assert ready(system, [first.id]) == [first.id]
    assert {chunk.document_id for chunk in system.vectors.chunks} == {str(first.id), str(second.id)}
    history, total = system.ingestion.versions(system.uid, first.id, 0, 1)
    assert total == 2 and history[0].id == second.id and history[0].version_info["is_current"]
    older, _ = system.ingestion.versions(system.uid, second.id, 1, 1)
    assert older[0].id == first.id


def test_activation_uses_compare_and_swap_and_never_activates_failed_pending_or_wrong_model(system):
    first = upload(system)
    second = upload(system, "pending", first.id, process=False)
    with pytest.raises(AppError) as error:
        system.ingestion.activate_version(system.uid, second.id, first.id)
    assert error.value.code == "DOCUMENT_NOT_READY"
    system.ingestion.process(system.uid, second.id)
    with pytest.raises(AppError) as error:
        system.ingestion.activate_version(system.uid, second.id, None)
    assert error.value.code == "DOCUMENT_VERSION_CONFLICT" and ready(system) == [first.id]
    system.ingestion.activate_version(system.uid, second.id, first.id)
    with pytest.raises(AppError) as error:
        system.ingestion.activate_version(system.uid, first.id, first.id)
    assert error.value.code == "DOCUMENT_VERSION_CONFLICT"
    with system.database.sessions() as session:
        row = Repository(session).document(system.uid, first.id)
        row.embedding_model = "other-model";session.commit()
    with pytest.raises(AppError) as error:
        system.ingestion.activate_version(system.uid, first.id, second.id)
    assert error.value.code == "DOCUMENT_NOT_READY"


def test_legacy_documents_are_virtual_v1_until_explicit_version_upload_without_migration(system):
    system.settings.document_versions_enabled = False
    first = upload(system)
    with system.database.sessions() as session:
        assert session.get(DocumentVersion, first.id) is None
    system.settings.document_versions_enabled = True
    before, total = system.ingestion.versions(system.uid, first.id, 0, 20)
    assert total == 1 and before[0].version_info["version_number"] == 1
    with system.database.sessions() as session:
        assert session.get(DocumentVersion, first.id) is None  # GET does not adopt/write.
    second = upload(system, "新版内容", first.id)
    assert second.version_info["series_id"] == first.id
    assert second.version_info["version_number"] == 2
    assert ready(system) == [first.id]


def test_failed_or_tampered_new_version_does_not_remove_current(system):
    first = upload(system)
    second = upload(system, "new source", first.id, process=False)
    system.ingestion.file_path(second.file_path).write_text("tampered", encoding="utf-8")
    system.ingestion.process(system.uid, second.id)
    second = system.ingestion.get(system.uid, second.id)
    assert second.status == "failed" and second.parser_metadata["error_code"] == "DOCUMENT_SOURCE_CHANGED"
    assert ready(system) == [first.id]
    assert {chunk.document_id for chunk in system.vectors.chunks} == {str(first.id)}


def test_duplicate_upload_is_rolled_back_without_consuming_a_version_or_leaving_file(system):
    first = upload(system)
    with pytest.raises(AppError) as error:
        upload(system, "NX-1 功率100W", first.id)
    assert error.value.code == "DUPLICATE_DOCUMENT"
    assert len(list(system.settings.upload_dir.rglob("*.md"))) == 1
    second = upload(system, "different source", first.id)
    assert second.version_info["version_number"] == 2


def test_cross_user_history_upload_activation_and_delete_are_scoped(system):
    first = upload(system)
    for operation in (
        lambda: system.ingestion.versions(system.other, first.id, 0, 20),
        lambda: system.ingestion.activate_version(system.other, first.id, first.id),
        lambda: system.ingestion.upload(system.other, "copy.md", None, BytesIO(b"copy"), base_document_id=first.id),
        lambda: system.ingestion.delete(system.other, first.id),
    ):
        with pytest.raises(AppError) as error:
            operation()
        assert error.value.code == "NOT_FOUND"
    assert ready(system) == [first.id]
    assert not (system.settings.upload_dir / str(system.other)).exists()


def test_deleting_current_does_not_silently_reactivate_old_or_reuse_numbers(system):
    first = upload(system)
    second = upload(system, "v2 source", first.id)
    system.ingestion.activate_version(system.uid, second.id, first.id)
    system.ingestion.delete(system.uid, second.id)
    assert ready(system) == []
    assert ready(system, [first.id]) == [first.id]
    third = upload(system, "v3 source", first.id)
    assert third.version_info["version_number"] == 3
    system.ingestion.activate_version(system.uid, first.id, None)
    assert ready(system) == [first.id]


def test_deleting_original_version_does_not_break_remaining_family(system):
    first = upload(system)
    second = upload(system, "v2 source", first.id)
    system.ingestion.activate_version(system.uid, second.id, first.id)
    system.ingestion.delete(system.uid, first.id)
    third = upload(system, "v3 source", second.id)
    assert third.version_info["version_number"] == 3
    assert ready(system) == [second.id]
    rows, count = system.ingestion.versions(system.uid, second.id, 0, 20)
    assert count == 2 and [row.version_info["version_number"] for row in rows] == [3, 2]


def test_failed_delete_keeps_compensation_state_and_never_selects_old_version(system):
    first = upload(system)
    second = upload(system, "v2 source", first.id)
    system.ingestion.activate_version(system.uid, second.id, first.id)
    with patch.object(system.vectors, "delete_document", side_effect=AppError("MILVUS_UNAVAILABLE", "fixture", 503)):
        with pytest.raises(AppError):
            system.ingestion.delete(system.uid, second.id)
    assert system.ingestion.get(system.uid, second.id).status == "deleting"
    assert ready(system) == [] and system.ingestion.file_path(second.file_path).exists()
    with pytest.raises(AppError) as error:
        upload(system, "v3 attempted during deletion", second.id)
    assert error.value.code == "DOCUMENT_DELETING"
    system.ingestion.delete(system.uid, second.id)
    third = upload(system, "v3 after deletion", first.id)
    assert third.version_info["version_number"] == 3


def test_chat_default_current_explicit_old_and_history_version_snapshot_survives_switch(system):
    first = upload(system)
    second = upload(system, "NX-1 功率200W", first.id)
    models = Models(ScriptedModel(responses=[
        call("delegate_rag", task="查功率"), call("search_knowledge", query="功率"), report(), final("功率100W[1]"),
        call("delegate_rag", task="查功率"), call("search_knowledge", query="功率"), report(), final("功率200W[1]"),
        call("delegate_rag", task="查旧版功率"), call("search_knowledge", query="功率"), report(), final("旧版功率100W[1]"),
    ]))
    workflow = SupervisorWorkflow(system.settings, RetrieverService(system.embeddings, system.vectors, 6), models, Web())
    sessions = SessionService(system.database)
    chat = ChatService(system.settings, system.database, sessions, workflow)
    conversation = sessions.create(system.uid, "版本问答")
    old = chat.answer_v2(system.uid, conversation.id, AgentChatInput("功率？", "knowledge"), "v1")
    assert old.citations[0]["document_version"]["version_number"] == 1
    system.ingestion.activate_version(system.uid, second.id, first.id)
    current = chat.answer_v2(system.uid, conversation.id, AgentChatInput("功率？", "knowledge"), "v2")
    assert current.citations[0]["document_version"]["version_number"] == 2
    historical = chat.answer_v2(system.uid, conversation.id, AgentChatInput("旧版功率？", "knowledge", (first.id,)), "v1-again")
    assert historical.citations[0]["document_id"] == str(first.id)
    system.ingestion.delete(system.uid, first.id)
    _, messages = sessions.get(system.uid, conversation.id)
    persisted = V2MessageResponse.model_validate(messages[1])
    assert persisted.citations[0].document_version.content_hash == first.content_hash
    assert persisted.citations[0].document_version.version_number == 1
    assert "100W" in persisted.citations[0].excerpt


def test_disabled_mode_preserves_upload_and_rejects_version_operations(system):
    system.settings.document_versions_enabled = False
    first = upload(system)
    with pytest.raises(AppError) as error:
        system.ingestion.versions(system.uid, first.id, 0, 20)
    assert error.value.code == "DOCUMENT_VERSIONS_DISABLED"


def test_disabling_management_does_not_mix_historical_versions_into_default_scope(system):
    first = upload(system)
    second = upload(system, "v2 source", first.id)
    system.ingestion.activate_version(system.uid, second.id, first.id)
    system.settings.document_versions_enabled = False
    with system.database.sessions() as session:
        assert Repository(session).ready_document_ids(system.uid, None, system.settings.embedding_model) == [second.id]


def test_additive_bootstrap_only_creates_version_tables():
    database = Database(Settings(_env_file=None))
    engine = MagicMock()
    database.__dict__["engine"] = engine
    with patch("app.infrastructure.persistence.database.inspect") as inspector, patch.object(Base.metadata, "create_all") as create:
        inspector.return_value.has_table.return_value = True
        database.initialize_document_versions()
    assert create.call_args.kwargs["tables"] == [DocumentSeries.__table__, DocumentVersion.__table__]


def test_http_version_payload_requires_explicit_compare_and_swap_field():
    from test_api_v2 import Services
    from app.main import create_app
    services = Services()
    with TestClient(create_app(services.settings, services)) as client:
        response = client.post(f"/api/v2/documents/{uuid4()}/activate", headers={"Authorization": "Bearer test"}, json={})
    assert response.status_code == 422


def test_http_upload_history_and_switch_use_owner_scope_and_version_metadata(system):
    from test_api_v2 import Services
    from app.main import create_app
    services = Services()
    services.current_user, services.settings, services.ingestion = system.uid, system.settings, system.ingestion
    first = upload(system)
    scheduled = []
    with patch.object(system.ingestion, "schedule", side_effect=lambda uid, did: scheduled.append((uid, did))):
        with TestClient(create_app(system.settings, services)) as client:
            headers = {"Authorization": "Bearer fixture"}
            new = client.post(f"/api/v2/documents/{first.id}/versions", headers=headers,
                              files={"file": ("new.md", b"power=200W", "text/markdown")})
            assert new.status_code == 202 and new.json()["version_info"]["version_number"] == 2
            from uuid import UUID
            second_id = UUID(new.json()["id"])
            system.ingestion.process(system.uid, second_id)
            listing = client.get(f"/api/v2/documents/{first.id}/versions", headers=headers)
            assert listing.json()["total"] == 2
            active = client.post(f"/api/v2/documents/{second_id}/activate", headers=headers,
                                 json={"expected_current_document_id": str(first.id)})
            assert active.status_code == 200 and active.json()["version_info"]["is_current"]
            services.current_user = system.other
            assert client.get(f"/api/v2/documents/{first.id}/versions", headers=headers).status_code == 404
    assert scheduled == [(system.uid, second_id)]
