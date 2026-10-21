from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.dependencies import current_user
from app.application.capabilities import CapabilityService
from app.core.errors import AppError
from app.domain.capabilities import CapabilityCatalog, HttpToolDeclaration, SkillDeclaration
from app.domain.contracts import AgentChatInput, AgentRunResult, ModelAnswer
from app.infrastructure.persistence.models import CapabilityConfiguration, CapabilityConfigurationVersion, User
from app.main import create_app
from test_chat_runs import system


@pytest.fixture
def control(system):
    s = system
    s.settings.capability_center_enabled = True
    s.settings.capability_secret_names = ["PUBLIC_DATA_TOKEN"]
    with s.database.sessions() as session:
        session.get(User, s.uid).role = "admin"
        raw = CapabilityCatalog().model_dump(mode="json")
        session.add(CapabilityConfiguration(id=1, revision=1, configuration=raw))
        session.add(CapabilityConfigurationVersion(revision=1, configuration=raw, change={"operation": "initialize"}))
        session.commit()
    s.capabilities = CapabilityService(s.settings, s.database)
    return s


def http_tool(**updates):
    return HttpToolDeclaration(**{"name": "public.lookup", "description": "Public lookup", "group": "public",
        "endpoint": "https://api.service.com/lookup", **updates})


def skill(**updates):
    return SkillDeclaration(**{"name": "research", "description": "Research", "instructions": "Cite real data.",
        "allowed_tools": ("public.lookup",), **updates})


def save(s, declaration, *, create=True, revision=None):
    return s.capabilities.change(s.uid, "skills" if isinstance(declaration, SkillDeclaration) else "tools", declaration.name,
        declaration=declaration, expected_revision=revision or s.capabilities.snapshot()[0], create=create)


def fails(code, function, *args, **kwargs):
    with pytest.raises(AppError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_create_update_keep_original_versions_and_share_across_instances(control):
    s = control
    save(s, http_tool())
    initial = save(s, skill())
    old = initial["catalog"]
    assert initial["revision"] == 3
    second = CapabilityService(s.settings, s.database)
    save(s, skill(instructions="New instructions"), create=False)
    revision, snapshot = second.snapshot()
    assert revision == 4 and snapshot.skills["research"].version == 2
    assert snapshot.skills["research"].declaration.instructions == "New instructions"
    history = second.history(s.uid)
    assert history["items"][1]["catalog"] == old
    assert history["items"][0]["created_by"] == s.uid
    assert history["items"][0]["change"]["operation"] == "update"
    assert second.history(s.uid, 1, 1)["items"][0]["revision"] == 3


def test_revision_conflict_and_create_update_semantics(control):
    s = control
    save(s, http_tool())
    fails("CAPABILITY_REVISION_CONFLICT", save, s, skill(), revision=1)
    fails("CAPABILITY_EXISTS", save, s, http_tool())
    fails("NOT_FOUND", save, s, http_tool(name="missing"), create=False)
    assert s.capabilities.snapshot()[0] == 2
    assert len(s.capabilities.history(s.uid)["items"]) == 2


def test_dependency_changes_are_atomic_and_retired_names_never_reused(control):
    s = control
    fails("INVALID_CAPABILITY_DEPENDENCIES", save, s, skill())
    save(s, http_tool())
    save(s, skill())
    fails("CAPABILITY_IN_USE", s.capabilities.change, s.uid, "tools", "public.lookup", expected_revision=3, delete=True)
    fails("INVALID_CAPABILITY_DEPENDENCIES", save, s, http_tool(enabled=False), create=False)
    assert s.capabilities.snapshot()[0] == 3
    s.capabilities.change(s.uid, "skills", "research", expected_revision=3, delete=True)
    s.capabilities.change(s.uid, "tools", "public.lookup", expected_revision=4, delete=True)
    fails("CAPABILITY_RETIRED", save, s, http_tool())
    fails("CAPABILITY_RETIRED", save, s, skill(allowed_tools=()))
    assert s.capabilities.snapshot()[0] == 5
    assert s.capabilities.history(s.uid)["items"][0]["change"]["operation"] == "delete"


def test_disabled_skill_still_prevents_deleting_its_referenced_tool(control):
    s = control
    save(s, http_tool())
    save(s, skill(enabled=False))
    save(s, http_tool(enabled=False), create=False)
    fails("CAPABILITY_IN_USE", s.capabilities.change, s.uid, "tools", "public.lookup", expected_revision=4, delete=True)
    assert not s.capabilities.public_catalog(s.uid)["skills"][0]["available"]


def test_builtin_tools_cannot_be_overwritten_or_deleted(control):
    s = control
    fails("BUILTIN_TOOL_PROTECTED", save, s, http_tool(name="delegate_rag"))
    fails("BUILTIN_TOOL_PROTECTED", s.capabilities.change, s.uid, "tools", "delegate_rag", expected_revision=1, delete=True)


def test_only_explicit_secret_names_are_accepted_and_never_return_values(control, monkeypatch):
    s = control
    monkeypatch.setenv("PUBLIC_DATA_TOKEN", "fixture-value-do-not-return")
    monkeypatch.setenv("DATABASE_PASSWORD", "unrelated-value")
    fails("CAPABILITY_SECRET_NOT_ALLOWED", save, s, http_tool(secret_headers={"Authorization": "DATABASE_PASSWORD"}))
    save(s, http_tool(secret_headers={"Authorization": "PUBLIC_DATA_TOKEN"}))
    save(s, skill(required_secrets=("PUBLIC_DATA_TOKEN",)))
    public = s.capabilities.public_catalog(s.uid)
    assert public["skills"][0]["available"]
    assert "fixture-value-do-not-return" not in str(public) + str(s.capabilities.control_plane(s.uid))
    custom = next(tool for tool in public["tools"] if tool["name"] == "public.lookup")
    assert "endpoint" not in custom and "secret_headers" not in custom
    monkeypatch.delenv("PUBLIC_DATA_TOKEN")
    assert s.capabilities.public_catalog(s.uid)["skills"][0]["availability_reason"] == "not_configured"


def test_role_and_pending_approval_availability(control):
    s = control
    save(s, http_tool(required_roles=("admin",)))
    save(s, skill())
    with s.database.sessions() as session:
        ordinary = session.scalar(select(User).where(User.id != s.uid))
        ordinary_id = ordinary.id
    assert s.capabilities.public_catalog(ordinary_id)["skills"][0]["availability_reason"] == "permission_required"
    fails("FORBIDDEN", s.capabilities.control_plane, ordinary_id)
    fails("FORBIDDEN", s.capabilities.history, ordinary_id)
    fails("FORBIDDEN", s.capabilities.change, ordinary_id, "skills", "research", expected_revision=3, declaration=skill())
    save(s, http_tool(requires_approval=True), create=False)
    assert s.capabilities.public_catalog(s.uid)["skills"][0]["availability_reason"] == "approval_not_available"


def test_disabled_feature_and_missing_migration_fail_closed(control):
    s = control
    s.settings.capability_center_enabled = False
    fails("CAPABILITIES_DISABLED", s.capabilities.public_catalog, s.uid)
    s.settings.capability_center_enabled = True
    with s.database.sessions() as session:
        session.delete(session.get(CapabilityConfiguration, 1)); session.commit()
    fails("CAPABILITY_MIGRATION_REQUIRED", s.capabilities.public_catalog, s.uid)


def test_http_routes_accept_json_arrays_enforce_admin_and_cas(control):
    s = control
    services = SimpleNamespace(settings=s.settings, database=s.database, capabilities=s.capabilities, close=lambda: None)
    app = create_app(s.settings, services)
    app.dependency_overrides[current_user] = lambda: s.uid
    with TestClient(app) as client:
        assert client.get("/api/v2/capability-center").status_code == 200
        body = {"expected_revision": 1, "declaration": http_tool().model_dump(mode="json")}
        assert client.post("/api/v2/capability-center/tools", json=body).status_code == 201
        assert client.post("/api/v2/capability-center/tools", json=body).status_code == 409
        body = {"expected_revision": 2, "declaration": skill().model_dump(mode="json")}
        assert client.post("/api/v2/capability-center/skills", json=body).status_code == 201
        assert client.put("/api/v2/capability-center/skills/wrong", json={**body, "expected_revision": 3}).status_code == 422
        assert client.get("/api/v2/capability-center/control-plane").headers["cache-control"] == "no-store"
        with s.database.sessions() as session:
            session.get(User, s.uid).role = "user"; session.commit()
        assert client.get("/api/v2/capability-center").status_code == 200
        assert client.get("/api/v2/capability-center/control-plane").status_code == 403
        assert client.post("/api/v2/capability-center/skills", json=body).status_code == 403
        assert client.get("/api/v2/capability-center/versions").status_code == 403


def test_frozen_skill_uses_retained_revision_after_catalog_edit(control):
    s = control
    save(s, http_tool())
    save(s, skill())
    frozen = s.capabilities.freeze(s.uid, skill_name="research", public_query="public weather")
    save(s, skill(instructions="Changed later"), create=False)
    save(s, http_tool(endpoint="https://new.service.com/lookup"), create=False)
    execution = s.capabilities.validate_execution(s.uid, frozen, tool_name="public.lookup")
    assert execution.revision == 3 and execution.catalog.skills["research"].version == 1
    assert execution.catalog.skills["research"].declaration.instructions == "Cite real data."
    assert execution.catalog.tools["public.lookup"].declaration.endpoint == "https://api.service.com/lookup"


def test_current_disablement_and_role_restrictions_revoke_frozen_execution(control):
    s = control
    save(s, http_tool())
    save(s, skill())
    frozen = s.capabilities.freeze(s.uid, skill_name="research", public_query="public weather")
    save(s, skill(enabled=False), create=False)
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)
    save(s, skill(required_roles=("admin",)), create=False)
    with s.database.sessions() as session:
        session.get(User, s.uid).role = "user"; session.commit()
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)


def test_normal_mode_freeze_omits_unavailable_tools_and_never_expands(control):
    s = control
    save(s, http_tool())
    frozen = s.capabilities.freeze(s.uid, search_mode="knowledge", public_query="public weather")
    assert frozen.allowed_tools == ("delegate_rag",)
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen, tool_name="public.lookup")
    save(s, skill())
    fails("SKILL_MODE_CONFLICT", s.capabilities.freeze, s.uid, skill_name="research", search_mode="knowledge", public_query="public weather")
    fails("HTTP_TOOL_PUBLIC_QUERY_REQUIRED", s.capabilities.freeze, s.uid, skill_name="research")


def test_frozen_catalog_requires_matching_immutable_history(control):
    s = control
    frozen = s.capabilities.freeze(s.uid)
    with s.database.sessions() as session:
        history = session.get(CapabilityConfigurationVersion, 1)
        history.configuration = {**history.configuration, "retired_skills": ["not-original"]}
        session.commit()
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)


def test_nested_mutation_cannot_bypass_frozen_catalog_digest(control):
    s = control
    save(s, http_tool())
    frozen = s.capabilities.freeze(s.uid, public_query="public weather")
    frozen.catalog.tools["public.lookup"].declaration.static_headers["X-Injected"] = "changed after admission"
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen, tool_name="public.lookup")


def test_queued_run_freezes_skill_and_records_exact_version_after_edit(control):
    s = control
    save(s, http_tool())
    save(s, skill())
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "capability-run", uuid4())
    frozen = run.execution_snapshot["context"]["capability_snapshot"]
    assert frozen["revision"] == 3 and frozen["allowed_tools"] == ["public.lookup"]
    save(s, skill(instructions="New version"), create=False)
    seen = []
    def execute(context, *args, **kwargs):
        seen.append(context.capability_snapshot)
        return AgentRunResult(ModelAnswer("hello", "fixture", "fixture"), "smalltalk", [], "auto", {})
    s.chat.agent_workflow = SimpleNamespace(run=execute)
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert seen == [frozen]
    assert message.run_metadata["capabilities"]["skill"] == {"name": "research", "version": 1}


def test_queued_run_revoked_skill_never_enters_workflow(control):
    s = control
    save(s, http_tool())
    save(s, skill())
    s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "revoked-skill", uuid4())
    save(s, skill(enabled=False), create=False)
    calls = []
    s.chat.agent_workflow = SimpleNamespace(run=lambda *args, **kwargs: calls.append(True))
    fails("CAPABILITY_SCOPE_CHANGED", s.chat.execute_prepared, s.store, s.store.claim_next())
    assert not calls


def test_public_http_skill_can_queue_in_web_mode_without_tavily_search(control):
    s = control
    save(s, http_tool())
    save(s, skill())
    assert not s.settings.web_search_enabled
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", search_mode="web", skill_name="research"), "http-without-search", uuid4())
    assert run.execution_snapshot["context"]["public_query"] == "public weather"
    assert run.execution_snapshot["context"]["capability_snapshot"]["allowed_tools"] == ["public.lookup"]


def test_slash_activation_freezes_skill_but_keeps_original_user_message(control):
    from app.infrastructure.persistence.models import ChatMessage
    s = control
    save(s, http_tool())
    save(s, skill())
    run = s.runs.start(s.uid, s.sid, AgentChatInput("/research public weather"), "slash-activation", uuid4())
    context = run.execution_snapshot["context"]
    assert context["question"] == context["public_query"] == "public weather"
    assert context["capability_snapshot"]["skill_name"] == "research"
    with s.database.sessions() as session:
        assert session.get(ChatMessage, run.user_message_id).content == "/research public weather"
    s.store.request_cancel(s.uid, run.id)
    fails("SKILL_ACTIVATION_CONFLICT", s.runs.start, s.uid, s.sid,
        AgentChatInput("/other public weather", skill_name="research"), "conflict", uuid4())
    fails("SKILL_QUERY_REQUIRED", s.runs.start, s.uid, s.sid, AgentChatInput("/research"), "missing-query", uuid4())
    assert s.capabilities.activation("/remember preference") == ("/remember preference", None)


def test_skill_description_is_role_checked_and_uses_frozen_instructions(control):
    s = control
    save(s, skill(allowed_tools=()))
    frozen = s.capabilities.freeze(s.uid, skill_name="research")
    save(s, skill(allowed_tools=(), instructions="New version"), create=False)
    assert s.capabilities.describe_skill(s.uid, "research")["instructions"] == "New version"
    assert s.capabilities.describe_skill(s.uid, "research", frozen=frozen)["instructions"] == "Cite real data."
    save(s, skill(allowed_tools=(), enabled=False), create=False)
    fails("SKILL_UNAVAILABLE", s.capabilities.describe_skill, s.uid, "research")


def test_resume_public_input_is_explicit_scoped_and_idempotent(control):
    from app.infrastructure.persistence.models import ChatRunCheckpoint
    s = control
    save(s, http_tool()); save(s, skill())
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "resume-public", uuid4())
    s.store.pause(s.store.claim_next(), {}, "Which city?")
    paused, _ = s.store.read(s.uid, run.id)
    assert paused.waiting["public_input_allowed"] is True
    args = (s.uid, run.id, paused.waiting["checkpoint_revision"], paused.resume_key, "PRIVATE_SUPPLEMENT")
    fails("WEB_QUERY_REQUIRED", s.store.resume, *args, public_query="password=fixture-private-secret")
    s.store.resume(*args, public_query="public weather Beijing")
    s.store.resume(*args, public_query="public weather Beijing")
    fails("RESUME_CONFLICT", s.store.resume, *args, public_query="public weather Shanghai")
    with s.database.sessions() as session:
        payload = session.get(ChatRunCheckpoint, run.id).payload
        assert payload["human_inputs"] == ["PRIVATE_SUPPLEMENT"]
        assert payload["public_inputs"] == ["public weather Beijing"]


def test_resume_cannot_add_public_tool_scope_to_an_unrelated_run(control):
    s = control
    run = s.runs.start(s.uid, s.sid, AgentChatInput("private knowledge", "knowledge"), "no-public-scope", uuid4())
    s.store.pause(s.store.claim_next(), {}, "Which document?")
    paused, _ = s.store.read(s.uid, run.id)
    assert not paused.waiting.get("public_input_allowed")
    fails("PUBLIC_INPUT_NOT_ALLOWED", s.store.resume, s.uid, run.id,
        paused.waiting["checkpoint_revision"], paused.resume_key, "private supplement", public_query="public query")


def test_context_estimate_counts_the_actual_skill_prompt_and_handles_empty_composition(control):
    from app.api.schemas import ContextEstimateRequest
    s = control
    save(s, skill(allowed_tools=(), instructions="Long required instructions. " * 3000))
    base = s.chat.estimate_context(s.uid, ContextEstimateRequest(search_mode="web"))
    explicit = s.chat.estimate_context(s.uid, ContextEstimateRequest(search_mode="web", skill_name="research"))
    slash = s.chat.estimate_context(s.uid, ContextEstimateRequest(search_mode="web", question="/research"))
    assert explicit["skill_name"] == slash["skill_name"] == "research"
    assert explicit["skill_version"] == 1
    assert explicit["estimated_input_tokens"] == slash["estimated_input_tokens"]
    assert explicit["estimated_input_tokens"] == base["estimated_input_tokens"] + explicit["skill_input_tokens"]
    assert explicit["recommendation"] == "skill_over_budget"


def test_runtime_sql_policy_is_verified_versioned_frozen_and_revocable(control, monkeypatch):
    from pydantic import SecretStr
    from app.domain.capabilities import SqlPolicy
    from app.infrastructure.sql import ReadOnlySQL
    s = control
    s.settings.sql_database_url = SecretStr("postgresql://reader:first@db.example/business")
    observed = []
    monkeypatch.setattr(ReadOnlySQL, "schema", lambda client, remaining: observed.append((client.settings, remaining)))
    monkeypatch.setattr(ReadOnlySQL, "close", lambda client: observed.append("closed"))
    policy = SqlPolicy(enabled=True, allowed_tables=("business.products",), timeout_seconds=2.0, max_rows=17)
    s.capabilities.update_runtime(s.uid, "sql", policy, 1)
    assert observed[0][0].sql_allowed_tables == ["business.products"] and observed[1] == "closed"
    frozen = s.capabilities.freeze(s.uid, search_mode="sql")
    assert frozen.sql_policy == policy
    s.settings.sql_database_url = SecretStr("postgresql://reader:rotated@db.example/business")
    assert s.capabilities.validate_execution(s.uid, frozen).sql_policy.max_rows == 17
    fails("CAPABILITY_REVISION_CONFLICT", s.capabilities.update_runtime, s.uid, "sql", policy, 1)
    s.capabilities.update_runtime(s.uid, "sql", policy.model_copy(update={"max_rows": 2}), 2)
    assert s.capabilities.validate_execution(s.uid, frozen).sql_policy.max_rows == 17
    s.settings.sql_database_url = SecretStr("postgresql://reader:rotated@other.example/business")
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)
    s.settings.sql_database_url = SecretStr("postgresql://reader:rotated@db.example/business")
    s.capabilities.update_runtime(s.uid, "sql", policy.model_copy(update={"allowed_tables": ("business.other",)}), 3)
    fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)
    assert s.capabilities.history(s.uid)["items"][0]["change"]["name"] == "sql"


def test_failed_sql_probe_closes_connection_and_preserves_catalog(control, monkeypatch):
    from app.domain.capabilities import SqlPolicy
    from app.infrastructure.sql import ReadOnlySQL
    s = control
    closed = []
    def reject(*args):
        raise AppError("SQL_CONFIGURATION_ERROR", "fixture rejects writable role", 503)
    monkeypatch.setattr(ReadOnlySQL, "schema", reject)
    monkeypatch.setattr(ReadOnlySQL, "close", lambda client: closed.append(True))
    fails("SQL_CONFIGURATION_ERROR", s.capabilities.update_runtime, s.uid, "sql",
        SqlPolicy(enabled=True, allowed_tables=("business.products",)), 1)
    assert closed == [True] and s.capabilities.snapshot()[0] == 1
    assert len(s.capabilities.history(s.uid)["items"]) == 1


def test_web_runtime_api_controls_catalog_and_revokes_existing_run(control):
    from app.domain.capabilities import WebPolicy
    s = control
    frozen = s.capabilities.freeze(s.uid, search_mode="web", public_query="public question")
    services = SimpleNamespace(settings=s.settings, database=s.database, capabilities=s.capabilities, close=lambda: None)
    app = create_app(s.settings, services)
    app.dependency_overrides[current_user] = lambda: s.uid
    with TestClient(app) as client:
        endpoint = "/api/v2/capability-center/runtime/web"
        body = {"expected_revision": 1, "declaration": {"enabled": False}}
        assert client.put(endpoint, json=body).status_code == 200
        assert client.put(endpoint, json=body).status_code == 409
        tool = next(item for item in client.get("/api/v2/capability-center").json()["tools"] if item["name"] == "delegate_web")
        assert tool["availability_reason"] == "disabled" and tool["version"] == 1
        capabilities = client.get("/api/v2/capabilities").json()
        assert not capabilities["web_search_enabled"] and not capabilities["web_read_enabled"]
        fails("CAPABILITY_SCOPE_CHANGED", s.capabilities.validate_execution, s.uid, frozen)
        fails("WEB_SEARCH_UNAVAILABLE", s.runs.start, s.uid, s.sid,
            AgentChatInput("page", search_mode="web", web_urls=("https://public.example/page",)), "disabled-web", uuid4())
        with s.database.sessions() as session:
            session.get(User, s.uid).role = "user"; session.commit()
        assert client.put(endpoint, json={**body, "expected_revision": 2}).status_code == 403
    assert s.capabilities.snapshot()[0] == 2


def test_claimed_run_uses_frozen_sql_client_and_closes_it(control, monkeypatch):
    from pydantic import SecretStr
    from app.domain.capabilities import SqlPolicy
    from app.application.run_execution import RunWorker
    from app.infrastructure.sql import ReadOnlySQL
    s = control
    s.settings.sql_database_url = SecretStr("postgresql://reader:fixture@db.example/business")
    monkeypatch.setattr(ReadOnlySQL, "schema", lambda *args: {})
    closed = []
    monkeypatch.setattr(ReadOnlySQL, "close", lambda client: closed.append(client.settings.sql_max_rows))
    policy = SqlPolicy(enabled=True, allowed_tables=("business.products",), max_rows=7)
    s.capabilities.update_runtime(s.uid, "sql", policy, 1)
    run = s.runs.start(s.uid, s.sid, AgentChatInput("count products", search_mode="sql"), "frozen-sql", uuid4())
    s.capabilities.update_runtime(s.uid, "sql", policy.model_copy(update={"max_rows": 3}), 2)
    observed = []
    original = s.workflow.run
    def execute(*args, **kwargs):
        observed.append((s.workflow.sql.settings.sql_allowed_tables, s.workflow.sql.settings.sql_max_rows))
        return original(*args, **kwargs)
    monkeypatch.setattr(s.workflow, "run", execute)
    s.workflow.release.set()
    assert RunWorker(s.store, s.chat).run_once()
    assert observed == [(["business.products"], 7)]
    assert closed[-1] == 7
    assert s.store.read(s.uid, run.id)[0].state == "succeeded"
