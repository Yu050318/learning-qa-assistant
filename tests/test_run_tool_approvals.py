import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user
from app.application.preflight import capability_preflight
from app.core.errors import AppError
from app.domain.capabilities import CapabilityExecution
from app.domain.contracts import AgentChatInput, RunContext
from app.domain.tool_approvals import RunToolApproval
from app.infrastructure.persistence.models import ChatRun, ChatSession, User
from app.main import create_app
from test_capability_control import control, fails, http_tool, save, skill
from test_chat_runs import system


@pytest.fixture
def approvals(control):
    s = control
    s.settings.tool_approvals_enabled = True
    save(s, http_tool(requires_approval=True))
    save(s, skill())
    return s


def request(**updates):
    return AgentChatInput(**{"question": "public weather", "skill_name": "research",
        "approved_tools": ("public.lookup",), "approval_catalog_revision": 3, **updates})


def start(s, **updates):
    return s.runs.start(s.uid, s.sid, request(**updates), "approval-fixture", uuid4())


def test_explicit_approval_is_atomic_run_bound_and_publicly_recorded(approvals):
    s = approvals
    public = s.capabilities.public_catalog(s.uid)
    assert public["skills"][0]["available"] and public["skills"][0]["approval_tools"] == ["public.lookup"]
    fails("TOOL_APPROVAL_REQUIRED", start, s, approved_tools=(), approval_catalog_revision=None)
    run = start(s)
    context = run.execution_snapshot["context"]
    grant = RunToolApproval.model_validate_json(json.dumps(context["tool_approval"]))
    execution = CapabilityExecution.model_validate_json(json.dumps(context["capability_snapshot"]))
    assert grant.run_id == run.id and grant.user_id == s.uid and grant.session_id == s.sid
    assert grant.organization_id is None and grant.catalog_hash == execution.catalog_hash
    assert execution.approved_tools == ("public.lookup",)
    s.capabilities.validate_approval(execution, grant, user_id=s.uid, session_id=s.sid, run_id=run.id, organization_id=None)
    for field in ("user_id", "session_id", "run_id", "organization_id"):
        identity = dict(user_id=s.uid, session_id=s.sid, run_id=run.id, organization_id=None)
        fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_approval, execution, grant, **{**identity, field: uuid4()})
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_approval, execution, None,
        user_id=s.uid, session_id=s.sid, run_id=run.id, organization_id=None)
    _, events = s.store.read(s.uid, run.id)
    approved = next(e for e in events if e.event == "tool_approval_granted")
    assert approved.payload["tool_names"] == ["public.lookup"]
    assert "public weather" not in json.dumps(approved.payload)


@pytest.mark.parametrize("updates,code", [
    ({"approval_catalog_revision": 2}, "CAPABILITY_REVISION_CONFLICT"),
    ({"approval_catalog_revision": None}, "INVALID_TOOL_APPROVAL"),
    ({"approved_tools": ()}, "INVALID_TOOL_APPROVAL"),
    ({"approved_tools": ("delegate_rag",)}, "INVALID_TOOL_APPROVAL"),
    ({"approved_tools": ("missing.tool",)}, "INVALID_TOOL_APPROVAL"),
    ({"approved_tools": ("public.lookup", "public.lookup")}, "INVALID_TOOL_APPROVAL"),
    ({"skill_name": None, "search_mode": "knowledge"}, "INVALID_TOOL_APPROVAL"),
])
def test_invalid_approvals_create_no_run_or_messages(approvals, updates, code):
    from sqlalchemy import func, select
    from app.infrastructure.persistence.models import ChatMessage
    s = approvals
    fails(code, start, s, **updates)
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChatRun)) == 0
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0


def test_unapproved_auto_scope_omits_tool_and_ordinary_user_cannot_approve(approvals):
    s = approvals
    frozen = s.capabilities.freeze(s.uid, public_query="public weather")
    assert "public.lookup" not in frozen.allowed_tools and not frozen.approved_tools
    fails("FORBIDDEN", s.capabilities.freeze, s.other, approved_tools=("public.lookup",),
        approval_catalog_revision=3, public_query="public weather")
    assert s.capabilities.public_catalog(s.other)["skills"][0]["availability_reason"] == "permission_required"


def test_feature_or_role_revocation_blocks_execution_and_resume(approvals):
    s = approvals
    run = start(s)
    frozen = run.execution_snapshot["context"]["capability_snapshot"]
    assert capability_preflight(s.settings, s.database)[0] == "pass"
    s.settings.tool_approvals_enabled = False
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)
    fails("CAPABILITY_SCOPE_CHANGED", capability_preflight, s.settings, s.database)
    s.settings.tool_approvals_enabled = True
    s.store.pause(s.store.claim_next(), {}, "Which public city?")
    paused, _ = s.store.read(s.uid, run.id)
    with s.database.sessions() as session:
        session.get(User, s.uid).role = "user"; session.commit()
    fails("CAPABILITY_SCOPE_CHANGED", s.store.resume, s.uid, run.id, paused.waiting["checkpoint_revision"],
        paused.resume_key, "public city")
    assert s.store.read(s.uid, run.id)[0].state == "waiting"


def test_new_approval_requirement_cannot_upgrade_an_existing_run(approvals):
    s = approvals
    save(s, http_tool(requires_approval=False), create=False)
    old = s.capabilities.freeze(s.uid, skill_name="research", public_query="public weather")
    assert not old.approved_tools
    save(s, http_tool(requires_approval=True), create=False)
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, old)


def test_grant_cannot_be_pasted_into_a_different_frozen_run_context(approvals):
    s = approvals
    run = start(s)
    data = run.execution_snapshot["context"]
    fields = dict(request_id="context-approval", user_id=s.uid, session_id=s.sid, question="public weather",
        search_mode="auto", allowed_document_ids=(), selected_provider="deepseek", public_query="public weather",
        web_enabled=False, run_id=run.id, tool_approval=data["tool_approval"], capability_snapshot=data["capability_snapshot"])
    context = RunContext(**fields)
    with pytest.raises(ValueError):
        replace(context, run_id=uuid4())
    with pytest.raises(ValueError):
        replace(context, tool_approval=None)


def test_api_accepts_only_revision_bound_explicit_admin_approval(approvals):
    s = approvals
    services = SimpleNamespace(settings=s.settings, database=s.database, capabilities=s.capabilities, runs=s.runs, close=lambda: None)
    app = create_app(s.settings, services)
    app.dependency_overrides[current_user] = lambda: s.uid
    path = f"/api/v2/sessions/{s.sid}/runs"
    body = {"question": "public weather", "skill_name": "research", "approved_tools": ["public.lookup"], "approval_catalog_revision": 3}
    with TestClient(app) as client:
        headers = {"Idempotency-Key": str(uuid4())}
        assert client.post(path, json={**body, "approval_catalog_revision": 2}, headers=headers).status_code == 409
        assert client.post(path, json={**body, "approved_tools": []}, headers=headers).status_code == 422
        created = client.post(path, json=body, headers=headers)
        assert created.status_code == 202
        assert client.post(path, json=body, headers=headers).json()["run_id"] == created.json()["run_id"]
        assert client.post(path, json={**body, "approval_catalog_revision": 4}, headers=headers).status_code == 409
        s.store.request_cancel(s.uid, UUID(created.json()["run_id"]))
        with s.database.sessions() as session:
            session.get(User, s.uid).role = "user"; session.commit()
        assert client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())}).status_code == 403


def test_disabled_approval_preserves_worker_fingerprint_and_frozen_catalog_hash(control):
    import hashlib
    from app.application.workers import WorkerRegistry
    from app.domain.capabilities import CapabilityCatalog, catalog_digest
    s = control
    legacy = {"schema_version": 1, "skills": {}, "tools": {}, "retired_skills": [], "retired_tools": [], "sql": None, "web": None}
    digest = hashlib.sha256(json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert catalog_digest(CapabilityCatalog()) == digest
    disabled = WorkerRegistry(s.settings, s.database, kind="run").configuration_hash()
    s.settings.tool_approvals_enabled = True
    assert WorkerRegistry(s.settings, s.database, kind="run").configuration_hash() != disabled
    s.settings.tool_approvals_enabled = False
    assert WorkerRegistry(s.settings, s.database, kind="run").configuration_hash() == disabled
    save(s, http_tool(requires_approval=True))
    fails("TOOL_APPROVALS_DISABLED", s.capabilities.freeze, s.uid, approved_tools=("public.lookup",),
        approval_catalog_revision=2, public_query="public weather")
