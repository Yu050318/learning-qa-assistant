"""Persistent control-plane invariants, independent of the execution worker."""
from datetime import datetime, timedelta, timezone
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.errors import AppError
from app.application.run_events import RunNotifications
from app.application.run_checkpoint import RunCheckpoint
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import ChatEventOutbox, ChatMessage, ChatRun, ChatRunCheckpoint, ChatRunEvent, ChatRunOperation, DocumentRecord
from test_chat_runs import system


@pytest.fixture
def control(system):
    s = system
    clock = SimpleNamespace(now=datetime(2026, 10, 17, tzinfo=timezone.utc))
    s.store.now = lambda session: clock.now
    run, _ = s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql"), "control-fixture", uuid4())
    with s.database.sessions() as session:
        row = session.get(ChatRun, run.id)
        row.state, row.payload, row.remaining_seconds = "queued", {"question": "fixture"}, 600
        session.commit()
    s.clock, s.run_id = clock, run.id
    return s


def fail(code, function, *args, **kwargs):
    with pytest.raises(AppError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def reclaim(s):
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    return s.store.claim_next()


def test_expired_owner_cannot_commit_checkpoint_receipt_or_heartbeat(control):
    s = control
    first = s.store.claim_next()
    assert first.generation == 1 and s.store.claim_next() is None
    s.store.begin_operation(first, "model/first", {"prompt": "fixture"}, readonly=True)
    second = reclaim(s)
    assert second.generation == 2 and second.token != first.token
    fail("RUN_LEASE_LOST", s.store.heartbeat, first)
    fail("RUN_LEASE_LOST", s.store.checkpoint, first, {"phase": "obsolete"})
    fail("RUN_LEASE_LOST", s.store.complete_operation, first, "model/first", {"answer": "obsolete"})
    s.store.heartbeat(second)
    assert s.store.load_checkpoint(second) == (0, {})


def test_completed_receipt_is_reused_after_owner_change_and_input_cannot_drift(control):
    s = control
    first = s.store.claim_next()
    assert s.store.begin_operation(first, "tool/read", {"query": "fixed"}, readonly=True) == (False, None)
    s.store.complete_operation(first, "tool/read", {"result": "persisted"}, checkpoint_payload={"phase": "retrieved"})
    second = reclaim(s)
    assert s.store.begin_operation(second, "tool/read", {"query": "fixed"}, readonly=True) == (True, {"result": "persisted"})
    assert s.store.load_checkpoint(second) == (1, {"phase": "retrieved"})
    fail("RUN_CHECKPOINT_DIVERGED", s.store.begin_operation, second, "tool/read", {"query": "changed"}, readonly=True)


def test_unknown_side_effect_is_never_automatically_reexecuted(control):
    s = control
    first = s.store.claim_next()
    s.store.begin_operation(first, "tool/save-memory", {"content": "fixture"}, readonly=False)
    second = reclaim(s)
    fail("TOOL_OUTCOME_UNCERTAIN", s.store.begin_operation, second, "tool/save-memory", {"content": "fixture"}, readonly=False)
    with s.database.sessions() as session:
        row = session.get(ChatRunOperation, (s.run_id, "tool/save-memory"))
        assert row.state == "uncertain" and row.attempts == 1


def test_readonly_unknown_operation_has_bounded_recovery(control):
    s = control
    claim = s.store.claim_next()
    for attempt in range(3):
        assert s.store.begin_operation(claim, "tool/read", {"query": "fixed"}, readonly=True) == (False, None)
        claim = reclaim(s)
    fail("RUN_RECOVERY_LIMIT", s.store.begin_operation, claim, "tool/read", {"query": "fixed"}, readonly=True)


def test_pause_resume_is_same_run_and_single_use_idempotent(control):
    s = control
    claim = s.store.claim_next()
    s.clock.now += timedelta(seconds=20)
    paused = s.store.pause(claim, {"phase": "clarification", "round": 0}, "Which region?")
    assert paused.state == "waiting" and paused.remaining_seconds == 580
    fail("RESOURCE_BUSY", s.store.create, s.uid, s.sid, AgentChatInput("another", "sql"), "another", uuid4())
    fail("NOT_FOUND", s.store.resume, s.other, s.run_id, 1, paused.resume_key, "domestic")
    s.clock.now += timedelta(seconds=300)
    resumed = s.store.resume(s.uid, s.run_id, 1, paused.resume_key, "domestic")
    assert resumed.id == s.run_id and resumed.state == "queued"
    assert s.store.resume(s.uid, s.run_id, 1, paused.resume_key, "domestic").id == s.run_id
    fail("RESUME_CONFLICT", s.store.resume, s.uid, s.run_id, 1, paused.resume_key, "overseas")
    next_claim = s.store.claim_next()
    revision, checkpoint = s.store.load_checkpoint(next_claim)
    assert next_claim.run_id == claim.run_id and revision == 2 and checkpoint["human_inputs"] == ["domestic"]
    with s.database.sessions() as session:
        row = session.get(ChatRun, s.run_id)
        assert row.remaining_seconds == 580
        assert session.scalar(select(func.count()).select_from(ChatRunEvent).where(ChatRunEvent.event == "run_resumed")) == 1


def test_cancel_waiting_run_invalidates_resume_and_running_cancel_is_acknowledged(control):
    s = control
    claim = s.store.claim_next()
    paused = s.store.pause(claim, {"phase": "clarification"}, "Which region?")
    cancelled = s.store.request_cancel(s.uid, s.run_id)
    assert cancelled.state == "cancelled"
    fail("RESUME_CONFLICT", s.store.resume, s.uid, s.run_id, 1, paused.resume_key, "domestic")
    sequence = cancelled.sequence
    assert s.store.request_cancel(s.uid, s.run_id).sequence == sequence


def test_running_cancel_blocks_further_steps_and_failure_becomes_cancelled(control):
    s = control
    claim = s.store.claim_next()
    assert s.store.request_cancel(s.uid, s.run_id).state == "cancelling"
    fail("RUN_CANCEL_REQUESTED", s.store.begin_operation, claim, "tool/next", {}, readonly=True)
    s.store.finish_failure(claim, RuntimeError("not exposed"))
    run, events = s.store.read(s.uid, s.run_id)
    assert run.state == "cancelled" and events[-1].event == "run_cancelled"
    assert "not exposed" not in str([event.payload for event in events])


def test_checkpoint_failure_rolls_back_receipt_and_events_have_outbox_entries(control):
    s = control
    claim = s.store.claim_next()
    s.store.begin_operation(claim, "tool/first", {}, readonly=True)
    with pytest.raises(ValueError):
        s.store.complete_operation(claim, "tool/first", {"result": "ok"}, checkpoint_payload={"value": float("nan")})
    with s.database.sessions() as session:
        assert session.get(ChatRunOperation, (s.run_id, "tool/first")).state == "started"
        assert session.get(ChatRunCheckpoint, s.run_id) is None
        events = session.scalar(select(func.count()).select_from(ChatRunEvent))
        outbox = session.scalar(select(func.count()).select_from(ChatEventOutbox))
        assert events == outbox == 3
        assert list(session.scalars(select(ChatRunEvent.event).order_by(ChatRunEvent.sequence))) == ['run_started','run_claimed','tool_started']
    assert s.store.dispatch_outbox(limit=1) == 1
    assert s.store.dispatch_outbox() == 2
    assert s.store.dispatch_outbox() == 0


def test_expired_human_checkpoint_cannot_resume(control):
    s = control
    paused = s.store.pause(s.store.claim_next(), {"phase": "clarification"}, "Which region?")
    s.clock.now += timedelta(seconds=s.settings.chat_run_wait_seconds + 1)
    fail("HITL_EXPIRED", s.store.resume, s.uid, s.run_id, 1, paused.resume_key, "domestic")
    assert s.store.read(s.uid, s.run_id)[0].state == "interrupted"


def test_worker_death_during_cancellation_does_not_leave_session_busy(control):
    s = control
    s.store.claim_next()
    s.store.request_cancel(s.uid, s.run_id)
    assert reclaim(s) is None
    assert s.store.read(s.uid, s.run_id)[0].state == "cancelled"


def test_recovery_cannot_reset_exhausted_execution_budget(control):
    s = control
    with s.database.sessions() as session:
        session.get(ChatRun, s.run_id).remaining_seconds = 10
        session.commit()
    s.store.claim_next()
    assert reclaim(s) is None
    run, events = s.store.read(s.uid, s.run_id)
    assert run.state == "failed" and events[-1].payload["code"] == "AGENT_TIMEOUT"


def test_notifications_only_wake_matching_run_and_clean_up_subscriptions():
    async def scenario():
        hub = RunNotifications(None)
        first, second = uuid4(), uuid4()
        async with hub.subscribe(first) as a, hub.subscribe(second) as b:
            hub.wake(first)
            await asyncio.wait_for(a.wait(), timeout=1)
            assert not b.is_set()
        assert hub.subscribers == {}
        hub.wake(first)
    asyncio.run(scenario())


def test_checkpoint_boundary_reuses_success_and_known_error_without_calling_provider(control):
    s = control
    checkpoint = RunCheckpoint(s.store, s.store.claim_next())
    calls = []

    def provider():
        calls.append("called")
        return {"answer": "fixture", "runtime": {"attempts": 1}}

    first = checkpoint.invoke("model/first", {"prompt": "fixture"}, provider, readonly=True)
    assert checkpoint.invoke("model/first", {"prompt": "fixture"}, provider, readonly=True) == first
    assert calls == ["called"]

    def denied():
        calls.append("denied")
        raise AppError("TOOL_NOT_ALLOWED", "This tool is not authorized", 403)

    for _ in range(2):
        fail("TOOL_NOT_ALLOWED", checkpoint.invoke, "tool/denied", {}, denied, readonly=True)
    assert calls == ["called", "denied"]


def test_queued_preparation_freezes_scope_and_model_and_creates_one_message_pair(system):
    from pydantic import SecretStr
    s = system
    document_id = uuid4()
    s.settings.deepseek_api_key = SecretStr("must-never-be-in-a-run-snapshot")
    with s.database.sessions() as session:
        session.add(DocumentRecord(id=document_id, user_id=s.uid, original_name="fixture.md", file_type=".md",
            file_path="fixture", content_hash="0" * 64, status="ready", embedding_model=s.settings.embedding_model, chunk_count=1))
        session.commit()
    key, preparations = uuid4(), []

    def prepare(session, run, payload):
        preparations.append(run.id)
        s.chat.prepare_run(session, run, payload)

    payload = AgentChatInput("Summarize the fixture", "knowledge")
    run, created = s.store.create(s.uid, s.sid, payload, "prepare-fixture", key, queued=True, prepare=prepare)
    assert created and run.state == "queued"
    snapshot = run.execution_snapshot
    assert snapshot["context"]["allowed_document_ids"] == [str(document_id)]
    assert snapshot["context"]["source_message_id"] == str(run.user_message_id)
    assert "must-never-be-in-a-run-snapshot" not in str(snapshot) and "deepseek_api_key" not in snapshot["settings"]
    previous_model = snapshot["settings"]["deepseek_model"]
    s.settings.deepseek_model = "changed-after-creation"
    again, created = s.store.create(s.uid, s.sid, payload, "retry-fixture", key, queued=True, prepare=prepare)
    assert not created and again.id == run.id and preparations == [run.id]
    assert again.execution_snapshot["settings"]["deepseek_model"] == previous_model
    with s.database.sessions() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.session_id == s.sid)).all()
        assert len(messages) == 2
        assert session.get(ChatMessage, run.user_message_id).role == "user"
        assert session.get(ChatMessage, run.message_id).role == "assistant"


def test_failed_preparation_rolls_back_run_messages_and_events(system):
    s = system

    def prepare(session, run, payload):
        s.chat.prepare_run(session, run, payload)
        raise AppError("PREPARATION_FAILED", "fixture", 503)

    fail("PREPARATION_FAILED", s.store.create, s.uid, s.sid, AgentChatInput("fixture", "sql"), "failed-prepare", uuid4(),
         queued=True, prepare=prepare)
    with s.database.sessions() as session:
        for model in (ChatRun, ChatMessage, ChatRunEvent, ChatEventOutbox):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_checkpoint_does_not_implicitly_stringify_secret_objects(control):
    from pydantic import SecretStr
    s = control
    claim = s.store.claim_next()
    with pytest.raises(TypeError):
        s.store.checkpoint(claim, {"credentials": SecretStr("fixture-never-persist")})
    assert s.store.load_checkpoint(claim) == (0, {})


def test_busy_session_delays_claim_without_resetting_remaining_budget(control):
    s = control
    claim = s.store.claim_next()
    s.clock.now += timedelta(seconds=3)
    s.store.release_busy(claim)
    assert s.store.claim_next() is None
    s.clock.now += timedelta(seconds=1)
    recovered = s.store.claim_next()
    assert recovered.generation == claim.generation + 1
    with s.database.sessions() as database:
        assert database.get(ChatRun, s.run_id).remaining_seconds == 597
    fail("RUN_LEASE_LOST", s.store.append, s.uid, s.run_id, "answer_delta", "obsolete", claim=claim)


def test_idle_worker_expires_waiting_runs_without_needing_a_browser_read(control):
    s = control
    claim = s.store.claim_next()
    s.store.pause(claim, {"phase": "clarification"}, "Which region?")
    s.clock.now += timedelta(seconds=s.settings.chat_run_wait_seconds + 1)
    assert s.store.claim_next() is None
    with s.database.sessions() as database:
        assert database.get(ChatRun, s.run_id).state == "interrupted"
    assert s.store.read(s.uid, s.run_id)[1][-1].payload["code"] == "HITL_EXPIRED"
