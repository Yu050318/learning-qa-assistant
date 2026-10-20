"""Workspace identity comes from the owned session, not retrieval selections."""
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.chat import ChatService
from app.application.runs import RunStore
from app.application.sessions import SessionService
from app.application.workspaces import list_workspaces
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import ChatSession, Organization
from app.main import create_app
from test_api_v2 import Services
from test_document_versions import system
from test_sharing import sharing
from test_document_tasks import published
from test_ingestion_worker import durable


@pytest.fixture
def spaces(sharing):
    s = sharing
    s.settings.workspaces_enabled = True
    s.database.settings = s.settings
    s.sessions = SessionService(s.database)
    s.chat = ChatService(s.settings, s.database, s.sessions, None)
    s.store = RunStore(s.settings, s.database)
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    return s


def frozen(s, user, conversation, payload=None):
    return s.store.create(user, conversation.id, payload or AgentChatInput("scope?", "knowledge"),
        "workspace", uuid4(), queued=True, prepare=s.chat.prepare_run)[0]


def test_sessions_and_counts_are_personal_per_workspace_even_for_team_admin(spaces):
    s = spaces
    personal = s.sessions.create(s.uid, "private")
    team = s.sessions.create(s.uid, "team private conversation", organization_id=s.oid)
    member = s.sessions.create(s.other, "member conversation", organization_id=s.oid)
    assert [row.id for row in s.sessions.list(s.uid, 0, 50)] == [personal.id]
    assert [row.id for row in s.sessions.list(s.uid, 0, 50, organization_id=s.oid)] == [team.id]
    with pytest.raises(AppError):
        s.sessions.get(s.uid, member.id)
    counts = {row["id"]: row["session_count"] for row in list_workspaces(s.settings, s.database, s.uid)}
    assert counts == {None: 1, s.oid: 1}
    with pytest.raises(AppError):
        s.sessions.create(s.outsider, "forged team", organization_id=s.oid)


def test_team_default_freezes_only_granted_libraries_and_excludes_personal_docs(spaces):
    s = spaces
    private = s.ingestion.upload(s.other, "personal.md", "text/markdown", BytesIO(b"private facts"))
    s.ingestion.process(s.other, private.id)
    team = s.sessions.create(s.other, "team", organization_id=s.oid)
    run = frozen(s, s.other, team)
    context = run.execution_snapshot["context"]
    assert context["organization_id"] == str(s.oid) and context["shared_only"]
    assert context["knowledge_base_ids"] == [str(s.kid)]
    assert context["allowed_document_ids"] == [str(s.did)]
    assert str(private.id) not in str(context)
    hidden = s.sharing.create_base(s.uid, s.oid, "not granted")["id"]
    second = s.sessions.create(s.other, "forged selection", organization_id=s.oid)
    with pytest.raises(AppError):
        frozen(s, s.other, second, AgentChatInput("scope?", "knowledge", knowledge_base_ids=(hidden,)))


def test_empty_team_never_falls_back_to_personal_documents(spaces):
    s = spaces
    private = s.ingestion.upload(s.other, "unshared.md", "text/markdown", BytesIO(b"must stay private"))
    s.ingestion.process(s.other, private.id)
    s.sharing.set_grant(s.uid, s.kid, s.other, None)
    team = s.sessions.create(s.other, "empty team", organization_id=s.oid)
    run = frozen(s, s.other, team)
    context = run.execution_snapshot["context"]
    assert context["allowed_document_ids"] == [] and context["knowledge_base_ids"] == []


def test_foreign_workspace_library_is_rejected_even_if_user_can_access_both(spaces):
    s = spaces
    other = s.sharing.create_organization(s.uid, "another team")["id"]
    library = s.sharing.create_base(s.uid, other, "another library")["id"]
    team = s.sessions.create(s.uid, "first team", organization_id=s.oid)
    with pytest.raises(AppError):
        frozen(s, s.uid, team, AgentChatInput("scope?", "knowledge", knowledge_base_ids=(library,)))


def test_revocation_blocks_session_and_run_reads_and_cannot_resume_with_personal_scope(spaces):
    s = spaces
    team = s.sessions.create(s.other, "team", organization_id=s.oid)
    run = frozen(s, s.other, team)
    s.sharing.remove_member(s.uid, s.oid, s.other)
    for action in (lambda: s.sessions.get(s.other, team.id), lambda: s.store.read(s.other, run.id),
                   lambda: s.sessions.rename(s.other, team.id, "private now")):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.status_code == 404


def test_archive_hides_space_without_deleting_or_declassifying_team_history(spaces):
    s = spaces
    team = s.sessions.create(s.uid, "retained team history", organization_id=s.oid)
    run = frozen(s, s.uid, team)
    s.sharing.delete_organization(s.uid, s.oid)
    assert s.sharing.organizations(s.uid) == [] and s.sharing.bases(s.uid) == []
    assert list_workspaces(s.settings, s.database, s.uid)[0]["session_count"] == 0
    with pytest.raises(AppError):
        s.store.read(s.uid, run.id)
    with s.database.sessions() as session:
        assert session.get(Organization, s.oid).archived_at is not None
        assert session.get(ChatSession, team.id).organization_id == s.oid
    assert s.ingestion.get(s.uid, s.did).status == "ready"


def test_private_session_rejects_team_selection_when_workspaces_are_enabled(spaces):
    s = spaces
    personal = s.sessions.create(s.uid, "private")
    with pytest.raises(AppError) as failure:
        frozen(s, s.uid, personal, AgentChatInput("scope?", "knowledge", knowledge_base_ids=(s.kid,)))
    assert failure.value.code == "INVALID_WORKSPACE_SCOPE"


def test_http_workspace_creation_and_session_list_preserve_authoritative_identity(spaces):
    s = spaces
    services = Services()
    services.current_user, services.settings, services.database, services.sessions = s.uid, s.settings, s.database, s.sessions
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(create_app(s.settings, services)) as client:
        created = client.post("/api/v2/sessions", headers=headers, json={"title": "team", "organization_id": str(s.oid)})
        assert created.status_code == 201 and created.json()["organization_id"] == str(s.oid)
        assert client.get("/api/v2/sessions", headers=headers).json() == []
        assert len(client.get(f"/api/v2/sessions?organization_id={s.oid}", headers=headers).json()) == 1
        assert len(client.get("/api/v2/workspaces", headers=headers).json()) == 2


@pytest.mark.parametrize("when", ["before_execution", "during_generation"])
def test_team_revocation_stops_prepared_execution_before_answer_publication(spaces, when):
    from types import SimpleNamespace
    from app.domain.contracts import AgentRunResult, ModelAnswer
    from app.infrastructure.persistence.models import ChatMessage
    s = spaces
    team = s.sessions.create(s.other, "team", organization_id=s.oid)
    run = frozen(s, s.other, team)
    calls = []
    def answer(*args, **kwargs):
        calls.append(True)
        s.sharing.remove_member(s.uid, s.oid, s.other)
        return AgentRunResult(ModelAnswer("must not publish", "fixture", "fixture"), "smalltalk", [], "knowledge", {})
    s.chat.agent_workflow = SimpleNamespace(run=answer)
    if when == "before_execution":
        s.sharing.remove_member(s.uid, s.oid, s.other)
    with pytest.raises(AppError) as failure:
        s.chat.execute_prepared(s.store, s.store.claim_next())
    assert failure.value.code == "DOCUMENT_SCOPE_CHANGED"
    assert len(calls) == (1 if when == "during_generation" else 0)
    with s.database.sessions() as session:
        assert session.get(ChatMessage, run.message_id).content == ""


def test_workspace_context_estimate_rejects_cross_team_selection(spaces):
    from app.api.schemas import ContextEstimateRequest
    s = spaces
    second = s.sharing.create_organization(s.uid, "other estimate")["id"]
    other_library = s.sharing.create_base(s.uid, second, "other")["id"]
    team = s.sessions.create(s.uid, "estimate", organization_id=s.oid)
    for payload in (ContextEstimateRequest(session_id=team.id, organization_id=second),
                    ContextEstimateRequest(organization_id=s.oid, knowledge_base_ids=[other_library])):
        with pytest.raises(AppError):
            s.chat.estimate_context(s.uid, payload)
    for mode in ("web", "sql"):
        with pytest.raises(AppError):
            s.chat.estimate_context(s.uid, ContextEstimateRequest(session_id=team.id, organization_id=second, search_mode=mode))
        with pytest.raises(AppError):
            s.chat.estimate_context(s.outsider, ContextEstimateRequest(organization_id=s.oid, search_mode=mode))


def test_revoked_workspace_worker_records_terminal_failure_without_publishing(spaces):
    from types import SimpleNamespace
    from app.application.run_execution import RunWorker
    from app.infrastructure.persistence.models import ChatMessage, ChatRun, ChatRunEvent
    s = spaces
    team = s.sessions.create(s.other, "revoked queued run", organization_id=s.oid)
    run = frozen(s, s.other, team)
    s.chat.agent_workflow = SimpleNamespace(run=lambda *args, **kwargs: pytest.fail("revoked run must not call the model"))
    s.sharing.remove_member(s.uid, s.oid, s.other)
    assert RunWorker(s.store, s.chat).run_once()
    with s.database.sessions() as session:
        result = session.get(ChatRun, run.id)
        assert result.state == "failed" and result.owner_token is None
        assert session.get(ChatMessage, run.message_id).content == ""
        terminal = session.get(ChatRunEvent, (run.id, result.sequence))
        assert terminal.event == "error" and terminal.payload["code"] == "DOCUMENT_SCOPE_CHANGED"


def test_workspace_configuration_requires_enabled_runtime_dependencies():
    from app.core.config import Settings
    with pytest.raises(ValueError, match="Workspaces require"):
        Settings(_env_file=None, workspaces_enabled=True)
    settings = Settings(_env_file=None, workspaces_enabled=True, sharing_enabled=True,
        document_versions_enabled=True, document_publication_enabled=True, ingestion_mode="durable", chat_runs_enabled=True)
    assert settings.workspaces_enabled


@pytest.fixture
def workspace_documents(published):
    from app.application.sharing import SharingService
    s = published
    s.settings.sharing_enabled = s.settings.workspaces_enabled = s.settings.chat_runs_enabled = True
    s.database.settings = s.settings
    s.sharing = SharingService(s.settings, s.database)
    s.oid = s.sharing.create_organization(s.uid, "document space")["id"]
    s.sharing.set_member(s.uid, s.oid, "version-other", "member")
    s.kid = s.sharing.create_base(s.uid, s.oid, "scoped documents")["id"]
    return s


def test_source_dedup_and_version_management_preserve_workspace_identity(workspace_documents):
    from app.infrastructure.persistence.repository import Repository
    s = workspace_documents
    personal = s.ingestion.upload(s.uid, "same.md", "text/markdown", BytesIO(b"same source"))
    team = s.ingestion.upload(s.uid, "same.md", "text/markdown", BytesIO(b"same source"), organization_id=s.oid)
    assert s.worker.run_once() and s.worker.run_once()
    assert personal.id != team.id and personal.content_hash == team.content_hash
    assert [doc.id for doc in s.ingestion.list(s.uid, 0, 50)] == [personal.id]
    assert [doc.id for doc in s.ingestion.list(s.uid, 0, 50, organization_id=s.oid)] == [team.id]
    for action in (lambda: s.ingestion.get(s.uid, team.id),
                   lambda: s.ingestion.get(s.uid, personal.id, organization_id=s.oid),
                   lambda: s.ingestion.upload(s.uid, "wrong.md", "text/markdown", BytesIO(b"new bytes"), base_document_id=team.id)):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.status_code == 404
    with s.database.sessions() as session:
        assert Repository(session).ready_document_ids(s.uid, None, s.settings.embedding_model, require_index=True) == [personal.id]
    s.sharing.bind(s.uid, s.kid, team.id)
    other = s.sharing.create_organization(s.uid, "separate team")["id"]
    other_library = s.sharing.create_base(s.uid, other, "different tenant")["id"]
    with pytest.raises(AppError) as failure:
        s.sharing.bind(s.uid, other_library, team.id)
    assert failure.value.code == "INVALID_WORKSPACE_SCOPE"
    s.sharing.bind(s.uid, other_library, personal.id)


def test_retirement_worker_can_settle_after_source_owner_loses_workspace_access(workspace_documents):
    from app.infrastructure.persistence.models import DocumentIndex, DocumentRecord
    s = workspace_documents
    doc = s.ingestion.upload(s.other, "team-owned.md", "text/markdown", BytesIO(b"member source"), organization_id=s.oid)
    assert s.worker.run_once()
    task = s.ingestion.delete(s.other, doc.id, organization_id=s.oid)
    assert task.organization_id == s.oid
    assert s.ingestion.tasks.list(s.other)["total"] == 0
    assert s.ingestion.tasks.list(s.other, organization_id=s.oid)["total"] == 2
    s.sharing.remove_member(s.uid, s.oid, s.other)
    for action in (lambda: s.ingestion.get(s.other, doc.id, organization_id=s.oid),
                   lambda: s.ingestion.tasks.detail(s.other, task.id, organization_id=s.oid)):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.status_code == 404
    assert s.worker.run_once()
    with s.database.sessions() as session:
        assert session.get(DocumentRecord, doc.id).status == "retired"
        assert session.get(DocumentIndex, doc.id).state == "cleaned"
        assert s.ingestion.tasks.get(session, s.other, task.id).state == "succeeded"


def test_http_document_upload_processing_and_tasks_share_explicit_workspace_scope(workspace_documents):
    s = workspace_documents
    services = Services()
    services.current_user, services.settings, services.database, services.ingestion = s.uid, s.settings, s.database, s.ingestion
    headers = {"Authorization": "Bearer fixture"}
    suffix = f"?organization_id={s.oid}"
    with TestClient(create_app(s.settings, services)) as client:
        result = client.post("/api/v2/documents" + suffix, headers=headers,
            files={"file": ("team.md", b"scoped upload", "text/markdown")})
        assert result.status_code == 202 and result.json()["organization_id"] == str(s.oid)
        did = result.json()["id"]
        assert client.get("/api/v2/documents", headers=headers).json()["total"] == 0
        assert client.get("/api/v2/documents" + suffix, headers=headers).json()["total"] == 1
        assert client.get(f"/api/v2/documents/{did}", headers=headers).status_code == 404
        assert client.get(f"/api/v2/documents/{did}/processing" + suffix, headers=headers).status_code == 200
        tasks = client.get("/api/v2/document-tasks" + suffix, headers=headers).json()
        assert tasks["total"] == 1 and tasks["items"][0]["organization_id"] == str(s.oid)
        assert client.get("/api/v2/document-tasks", headers=headers).json()["total"] == 0
        services.current_user = s.other
        assert client.get(f"/api/v2/documents/{did}" + suffix, headers=headers).status_code == 404


def test_team_graph_uses_frozen_published_index_and_never_personal_fallback(published, monkeypatch):
    from app.application.retriever import RetrieverService
    from app.application.sharing import SharingService
    from app.workflows.supervisor import SupervisorWorkflow
    from test_agent_v2 import Models, ScriptedModel, Web, call, final, report
    s = published
    s.settings.sharing_enabled = s.settings.workspaces_enabled = s.settings.chat_runs_enabled = True
    s.database.settings = s.settings
    s.database.resource_lock = lambda namespace, identity: s.database.document_lock(identity)
    s.sharing = SharingService(s.settings, s.database)
    s.sessions = SessionService(s.database)
    shared = s.ingestion.upload(s.uid, "team.md", "text/markdown", BytesIO("NX-1 功率100W".encode()))
    assert s.worker.run_once()
    private = s.ingestion.upload(s.other, "private.md", "text/markdown", BytesIO(b"PRIVATE must not appear"))
    assert s.worker.run_once()
    oid = s.sharing.create_organization(s.uid, "typed team")["id"]
    s.sharing.set_member(s.uid, oid, "version-other", "member")
    kid = s.sharing.create_base(s.uid, oid, "published facts")["id"]
    s.sharing.bind(s.uid, kid, shared.id)
    s.sharing.set_grant(s.uid, kid, s.other, "viewer")
    captured = []
    def index_backend(target):
        assert target.document_id == shared.id and target.user_id == s.uid
        assert target.collection_name == original_collection
        captured.append(target)
        return RetrieverService(s.embeddings, s.vectors, 6)
    retriever = RetrieverService(s.embeddings, s.vectors, 6, index_factory=index_backend)
    model = ScriptedModel(responses=[call("delegate_rag", task="功率"), call("search_knowledge", query="NX-1 功率"), report(), final("功率100W[1]")])
    monkeypatch.setattr("app.infrastructure.llms.providers.ModelRouter", lambda *args: Models(model))
    workflow = SupervisorWorkflow(s.settings, retriever, Models(model), Web(), sharing=s.sharing)
    s.chat = ChatService(s.settings, s.database, s.sessions, workflow)
    s.store = RunStore(s.settings, s.database)
    team = s.sessions.create(s.other, "team indexed answer", organization_id=oid)
    run = frozen(s, s.other, team)
    assert run.execution_snapshot["context"]["allowed_document_ids"] == [str(shared.id)]
    assert str(private.id) not in str(run.execution_snapshot["context"])
    original_collection = s.settings.milvus_collection
    s.settings.milvus_collection = "new_default_index"
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert message.content == "功率100W[1]"
    assert message.citations[0]["document_id"] == str(shared.id) and len(captured) == 1
