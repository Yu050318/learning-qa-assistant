from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.chat import ChatService
from app.application.retriever import RetrieverService
from app.application.sessions import SessionService
from app.application.sharing import SharingService
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import User
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report
from test_document_versions import system


@pytest.fixture
def sharing(system):
    system.database.sessions.kw["bind"].commit()
    system.settings.sharing_enabled = True
    system.sharing = SharingService(system.settings, system.database)
    with system.database.sessions() as session:
        outsider = User(username="outside", password_hash="fixture")
        admin = User(username="org-admin", password_hash="fixture")
        session.add_all([outsider, admin]);session.commit()
    system.outsider, system.admin = outsider.id, admin.id
    org = system.sharing.create_organization(system.uid, "开发组织")
    system.oid = org["id"]
    system.sharing.set_member(system.uid, system.oid, "version-other", "member")
    system.kid = system.sharing.create_base(system.uid, system.oid, "产品知识库")["id"]
    doc = system.ingestion.upload(system.uid, "shared.md", "text/markdown", BytesIO("NX-1 功率100W".encode()))
    system.ingestion.process(system.uid, doc.id)
    system.did = doc.id
    system.sharing.bind(system.uid, system.kid, doc.id)
    yield system


def denied(function, *args, status=404, **kwargs):
    with pytest.raises(AppError) as failure:
        function(*args, **kwargs)
    assert failure.value.status_code == status, failure.value.code


def test_membership_and_per_library_grants_are_both_required(sharing):
    s = sharing
    assert s.sharing.bases(s.other) == []
    denied(s.sharing.documents, s.other, s.kid)
    denied(s.sharing.documents, s.outsider, s.kid)
    denied(s.sharing.set_grant, s.uid, s.kid, s.outsider, "viewer")
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    assert s.sharing.bases(s.other)[0]["role"] == "viewer"
    assert s.sharing.documents(s.other, s.kid)[0]["id"] == s.did
    assert s.sharing.scope(s.other, [s.kid]) == {s.did:s.uid}
    denied(s.ingestion.get, s.other, s.did)
    denied(s.ingestion.retry, s.other, s.did)
    denied(s.ingestion.delete, s.other, s.did)
    s.sharing.set_grant(s.uid, s.kid, s.other, None)
    denied(s.sharing.scope, s.other, [s.kid])


def test_membership_removal_cascades_grants_and_readding_does_not_restore_access(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "editor")
    s.sharing.remove_member(s.uid, s.oid, s.other)
    denied(s.sharing.documents, s.other, s.kid)
    s.sharing.set_member(s.uid, s.oid, "version-other", "member")
    denied(s.sharing.documents, s.other, s.kid)
    assert s.ingestion.get(s.uid, s.did).status == "ready"


def test_roles_cannot_escalate_or_remove_owner(sharing):
    s = sharing
    denied(s.sharing.create_base, s.other, s.oid, "不能创建", status=403)
    denied(s.sharing.set_member, s.other, s.oid, "outside", "admin", status=403)
    s.sharing.set_member(s.uid, s.oid, "org-admin", "admin")
    denied(s.sharing.set_member, s.admin, s.oid, "outside", "admin", status=403)
    denied(s.sharing.remove_member, s.admin, s.oid, s.uid, status=403)
    denied(s.sharing.delete_organization, s.admin, s.oid, status=403)
    s.sharing.set_member(s.admin, s.oid, "outside", "member")
    assert s.sharing.bases(s.admin)[0]["role"] == "admin"
    denied(s.sharing.set_member, s.uid, s.oid, "version-owner", "member", status=403)


def test_editor_publishes_only_own_ready_sources_and_unpublish_preserves_bytes(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    denied(s.sharing.bind, s.other, s.kid, s.did, status=403)
    s.sharing.set_grant(s.uid, s.kid, s.other, "editor")
    denied(s.sharing.bind, s.other, s.kid, s.did)
    doc = s.ingestion.upload(s.other, "reader.md", "text/markdown", BytesIO(b"reader source"))
    denied(s.sharing.bind, s.other, s.kid, doc.id, status=409)
    s.ingestion.process(s.other, doc.id)
    s.sharing.bind(s.other, s.kid, doc.id)
    assert len(s.sharing.documents(s.other, s.kid)) == 2
    s.sharing.bind(s.other, s.kid, s.did, remove=True)
    assert s.ingestion.file_path(s.ingestion.get(s.uid, s.did).file_path).is_file()
    s.ingestion.delete(s.other, doc.id)
    assert s.sharing.documents(s.other, s.kid) == []


def test_version_sharing_is_explicit_and_default_uses_only_published_current(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    newer = s.ingestion.upload(s.uid, "new.md", "text/markdown", BytesIO(b"NX-1 200W"), base_document_id=s.did)
    s.ingestion.process(s.uid, newer.id)
    s.ingestion.activate_version(s.uid, newer.id, s.did)
    assert s.sharing.scope(s.other, [s.kid]) == {s.did:s.uid}
    assert s.sharing.scope(s.other, [s.kid], [s.did]) == {s.did:s.uid}
    denied(s.sharing.scope, s.other, [s.kid], [newer.id])
    s.sharing.bind(s.uid, s.kid, newer.id, expected_current=s.did)
    s.settings.document_versions_enabled = False
    assert s.sharing.scope(s.other, [s.kid]) == {newer.id:s.uid}
    assert s.sharing.scope(s.other, [s.kid], [s.did]) == {s.did:s.uid}
    rows = {row["id"]: row for row in s.sharing.documents(s.other, s.kid)}
    assert rows[newer.id]["is_current"] and not rows[s.did]["is_current"]
    assert rows[s.did]["current_document_id"] == newer.id
    # Withdrawing the current publication never silently restores history.
    s.sharing.bind(s.uid, s.kid, newer.id, remove=True)
    assert s.sharing.scope(s.other, [s.kid]) == {}
    assert s.sharing.scope(s.other, [s.kid], [s.did]) == {s.did:s.uid}


def test_shared_publish_requires_expected_version_and_is_independent_per_library(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    newer = s.ingestion.upload(s.uid, "new.md", "text/markdown", BytesIO(b"private v2"), base_document_id=s.did)
    s.ingestion.process(s.uid, newer.id)
    second = s.sharing.create_base(s.uid, s.oid, "independent publication")["id"]
    s.sharing.set_grant(s.uid, second, s.other, "viewer")
    s.sharing.bind(s.uid, second, newer.id)
    assert s.sharing.scope(s.other, [second]) == {newer.id:s.uid}
    assert s.sharing.scope(s.other, [s.kid]) == {s.did:s.uid}
    with pytest.raises(AppError) as failure:
        s.sharing.bind(s.uid, s.kid, newer.id)
    assert failure.value.code == "KNOWLEDGE_VERSION_CONFLICT"
    assert len(s.sharing.documents(s.uid, s.kid)) == 1
    s.sharing.bind(s.uid, s.kid, newer.id, expected_current=s.did)
    with pytest.raises(AppError) as failure:
        s.sharing.bind(s.uid, s.kid, s.did, expected_current=s.did)
    assert failure.value.code == "KNOWLEDGE_VERSION_CONFLICT"
    assert s.sharing.scope(s.other, [s.kid]) == {newer.id:s.uid}


def test_personal_version_switch_after_editor_revocation_never_republishes_team_data(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "editor")
    own = s.ingestion.upload(s.other, "member.md", "text/markdown", BytesIO(b"shared member v1"))
    s.ingestion.process(s.other, own.id)
    s.sharing.bind(s.other, s.kid, own.id)
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    newer = s.ingestion.upload(s.other, "member-new.md", "text/markdown", BytesIO(b"private member v2"), base_document_id=own.id)
    s.ingestion.process(s.other, newer.id)
    s.ingestion.activate_version(s.other, newer.id, own.id)
    denied(s.sharing.bind, s.other, s.kid, newer.id, expected_current=own.id, status=403)
    assert s.sharing.scope(s.uid, [s.kid], [own.id]) == {own.id:s.other}
    assert newer.id not in {row["id"] for row in s.sharing.documents(s.uid, s.kid)}


def test_full_supervisor_shared_rag_preserves_owner_filters_and_version_citations(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    model = ScriptedModel(responses=[call("delegate_rag", task="查询功率"), call("search_knowledge", query="功率"), report(), final("功率100W[1]")])
    retriever = RetrieverService(s.embeddings, s.vectors, 6)
    workflow = SupervisorWorkflow(s.settings, retriever, Models(model), Web(), sharing=s.sharing)
    sessions = SessionService(s.database)
    conversation = sessions.create(s.other, "共享问答")
    chat = ChatService(s.settings, s.database, sessions, workflow)
    with patch.object(s.vectors, "search", wraps=s.vectors.search) as search:
        result = chat.answer_v2(s.other, conversation.id, AgentChatInput("功率？", "knowledge", knowledge_base_ids=(s.kid,)), "shared-fixture")
    assert search.call_args.args[0] == s.uid
    assert result.citations[0]["document_id"] == str(s.did)
    assert result.citations[0]["document_version"]["version_number"] == 1
    assert len(sessions.get(s.other, conversation.id)[1]) == 2


def test_revocation_during_answer_blocks_final_message(sharing):
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    retriever = RetrieverService(s.embeddings, s.vectors, 6)
    original = retriever.retrieve

    def retrieve(*args, **kwargs):
        hits = original(*args, **kwargs)
        s.sharing.set_grant(s.uid, s.kid, s.other, None)
        return hits

    model = ScriptedModel(responses=[call("delegate_rag", task="查询功率"), call("search_knowledge", query="功率"), report(), final("功率100W[1]")])
    sessions = SessionService(s.database)
    conversation = sessions.create(s.other, "撤销测试")
    workflow = SupervisorWorkflow(s.settings, retriever, Models(model), Web(), sharing=s.sharing)
    chat = ChatService(s.settings, s.database, sessions, workflow)
    with patch.object(retriever, "retrieve", side_effect=retrieve):
        denied(chat.answer_v2, s.other, conversation.id, AgentChatInput("功率？", "knowledge", knowledge_base_ids=(s.kid,)), "revoke-fixture")
    assert [message.role for message in sessions.get(s.other, conversation.id)[1]] == ["user"]


def test_http_scope_and_role_validation(sharing):
    from test_api_v2 import Services
    from app.main import create_app
    s = sharing
    services = Services()
    services.settings, services.sharing, services.current_user = s.settings, s.sharing, s.uid
    headers = {"Authorization":"Bearer fixture"}
    with TestClient(create_app(s.settings, services)) as client:
        assert client.get("/api/v2/organizations", headers=headers).json()[0]["id"] == str(s.oid)
        assert client.put(f"/api/v2/knowledge-bases/{s.kid}/grants/{s.other}", headers=headers, json={"role":"admin"}).status_code == 422
        url = f"/api/v2/knowledge-bases/{s.kid}/documents/{s.did}"
        assert client.put(url, headers=headers, json={}).status_code == 422
        assert client.put(url, headers=headers, json={"expected_current_document_id":None}).status_code == 409
        assert client.put(url, headers=headers, json={"expected_current_document_id":str(s.did)}).status_code == 204
        services.current_user = s.outsider
        assert client.get(f"/api/v2/knowledge-bases/{s.kid}/documents", headers=headers).status_code == 404
        assert client.post(f"/api/v2/sessions/{uuid4()}/messages", headers=headers, json={"question":"q","search_mode":"sql","knowledge_base_ids":[str(s.kid)]}).status_code == 422


def test_organization_delete_preserves_source_and_other_tenant(sharing):
    s = sharing
    second = s.sharing.create_organization(s.outsider, "另一个租户")
    other_base = s.sharing.create_base(s.outsider, second["id"], "独立资料")
    denied(s.sharing.documents, s.uid, other_base["id"])
    s.sharing.delete_organization(s.uid, s.oid)
    assert s.ingestion.get(s.uid, s.did).status == "ready"
    assert s.sharing.bases(s.outsider)[0]["id"] == other_base["id"]


def test_mixed_owner_retrieval_embeds_once_reranks_once_and_rejects_forged_owner(sharing):
    from dataclasses import replace
    from unittest.mock import MagicMock
    from app.domain.contracts import SearchHit
    s = sharing
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    own = s.ingestion.upload(s.other, "personal.md", "text/markdown", BytesIO(b"personal distinct fact"))
    s.ingestion.process(s.other, own.id)
    owners = s.sharing.scope(s.other, [s.kid])
    reranker = SimpleNamespace(rerank=MagicMock(side_effect=lambda query, hits, top, **kwargs:hits[:top]))
    expansion = MagicMock(side_effect=lambda uid, ids, hits:hits)
    retriever = RetrieverService(s.embeddings, s.vectors, 6, reranker=reranker, context_expander=expansion)
    with patch.object(s.embeddings, "embed_query", wraps=s.embeddings.embed_query) as embed:
        hits = retriever.retrieve(s.other, "fact", list(owners), document_owners=owners)
    assert len(hits) == 2 and embed.call_count == reranker.rerank.call_count == 1 and expansion.call_count == 2
    assert {hit.chunk.user_id for hit in hits} == {str(s.uid),str(s.other)}
    poisoned = replace(hits[0].chunk, user_id=str(s.outsider))
    with patch.object(s.vectors, "search", return_value=[SearchHit(poisoned,.99)]):
        denied(retriever.retrieve, s.other, "fact", list(owners), document_owners=owners, status=503)


def test_disabled_sharing_does_not_grant_access_or_change_private_sources(sharing):
    s = sharing
    s.settings.sharing_enabled = False
    denied(s.sharing.scope, s.other, [s.kid], status=503)
    assert s.ingestion.get(s.uid, s.did).status == "ready"


def test_shared_answers_work_with_persistent_runs_without_reexecution(sharing, monkeypatch, tmp_path):
    import sqlite3
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    from app.application.runs import RunService
    from app.application.run_execution import RunWorker
    s = sharing
    # Graph checkpoints commit/rollback independently of graph tool sessions.
    # The version fixture's one externally bound connection cannot model this.
    original_sessions = s.database.sessions
    source = original_sessions.kw["bind"]
    source.commit()
    path = tmp_path / "shared-run.sqlite"
    target = sqlite3.connect(path)
    try:
        source.connection.driver_connection.backup(target, name="rag")
    finally:
        target.close()
    engine = create_engine("sqlite://", poolclass=NullPool, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def attach(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("ATTACH DATABASE ? AS rag", (str(path),))

    s.database.sessions = sessionmaker(bind=engine, expire_on_commit=False)
    s.settings.chat_runs_enabled = True
    s.sharing.set_grant(s.uid, s.kid, s.other, "viewer")
    model = ScriptedModel(responses=[call("delegate_rag", task="查询功率"), call("search_knowledge", query="功率"), report(), final("功率100W[1]")])
    sessions = SessionService(s.database)
    conversation = sessions.create(s.other, "共享流式")
    workflow = SupervisorWorkflow(s.settings, RetrieverService(s.embeddings,s.vectors,6), Models(model), Web(), sharing=s.sharing)
    chat = ChatService(s.settings,s.database,sessions,workflow)
    runs = RunService(s.settings,s.database,chat)
    payload = AgentChatInput("功率？","knowledge",knowledge_base_ids=(s.kid,))
    key = uuid4()
    try:
        monkeypatch.setattr("app.infrastructure.llms.providers.ModelRouter", lambda *args: Models(model))
        run = runs.start(s.other,conversation.id,payload,"shared-run",key)
        assert run.state == "queued" and model.position == 0
        # A team publication change affects new runs, not an already frozen V1.
        newer = s.ingestion.upload(s.uid, "new-shared.md", "text/markdown", BytesIO(b"NX-1 200W"), base_document_id=s.did)
        s.ingestion.process(s.uid, newer.id)
        s.ingestion.activate_version(s.uid, newer.id, s.did)
        s.sharing.bind(s.uid, s.kid, newer.id, expected_current=s.did)
        assert s.sharing.scope(s.other, [s.kid]) == {newer.id:s.uid}
        assert RunWorker(runs.store, chat).run_once()
        state, rows = runs.store.read(s.other,run.id)
        assert state.state == "succeeded", rows[-1].payload
        assert rows[-1].payload["citations"][0]["document_id"] == str(s.did)
        assert runs.start(s.other,conversation.id,payload,"retry",key).id == run.id
        assert len(sessions.get(s.other,conversation.id)[1]) == 2
    finally:
        runs.close()
        s.database.sessions = original_sessions
        engine.dispose()
