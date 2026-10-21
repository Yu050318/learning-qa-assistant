from app.application.preflight import capability_preflight
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import CapabilityConfigurationVersion, ChatRun, utcnow
from test_capability_control import control, http_tool, save, skill
from test_chat_runs import system
from uuid import uuid4
from datetime import timedelta


def test_catalog_and_immutable_history_are_checked_without_writes(control):
    s = control
    assert capability_preflight(s.settings, s.database)[:2] == ("pass", "CAPABILITY_CATALOG_READY")
    with s.database.sessions() as session:
        session.get(CapabilityConfigurationVersion, 1).configuration = {"schema_version": 1, "skills": {}, "tools": {}, "retired_skills": ["tampered"], "retired_tools": []}
        session.commit()
    assert capability_preflight(s.settings, s.database)[:2] == ("fail", "CAPABILITY_HISTORY_INVALID")


def test_enabling_requires_durable_run_and_refuses_unfrozen_pending_run(control):
    s = control
    s.settings.chat_runs_enabled = False
    assert capability_preflight(s.settings, s.database)[1] == "CAPABILITY_DURABLE_RUN_REQUIRED"
    s.settings.chat_runs_enabled = True
    s.settings.capability_center_enabled = False
    run = s.runs.start(s.uid, s.sid, AgentChatInput("legacy", "knowledge"), "unfrozen", uuid4())
    s.settings.capability_center_enabled = True
    assert capability_preflight(s.settings, s.database)[1] == "CAPABILITY_RUN_SNAPSHOT_REQUIRED"
    with s.database.sessions() as session:
        assert session.get(ChatRun, run.id).execution_snapshot["context"]["capability_snapshot"] is None
        session.get(ChatRun, run.id).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert capability_preflight(s.settings, s.database)[0] == "pass"


def test_disabling_with_active_frozen_run_is_blocked(control):
    s = control
    save(s, http_tool()); save(s, skill())
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "frozen", uuid4())
    assert capability_preflight(s.settings, s.database)[0] == "pass"
    s.settings.capability_center_enabled = False
    s.settings.chat_runs_enabled = False
    assert capability_preflight(s.settings, s.database)[1] == "CAPABILITY_FEATURE_DISABLED"
    s.store.request_cancel(s.uid, run.id)
    assert capability_preflight(s.settings, s.database)[0] == "disabled"
