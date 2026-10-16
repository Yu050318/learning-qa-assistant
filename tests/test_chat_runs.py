from contextlib import contextmanager
from datetime import timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from uuid import uuid4, UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.application.chat import ChatService
from app.application.runs import RunService, RunStore
from app.application.sessions import SessionService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, AgentRunResult, ModelAnswer
from app.infrastructure.persistence.models import Base, ChatMessage, ChatRun, ChatRunEvent, User
from app.main import create_app
from test_api_v2 import Services


@pytest.fixture
def system(tmp_path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def attach(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("ATTACH DATABASE ? AS rag", (str(tmp_path / "runs.db"),))

    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, chat_runs_enabled=True, chat_run_workers=2)

    @contextmanager
    def lock(*args):
        yield lambda: None

    database = SimpleNamespace(settings=settings, sessions=sessionmaker(bind=engine, expire_on_commit=False), resource_lock=lock)
    with database.sessions() as session:
        user, other = User(username="run-owner", password_hash="fixture"), User(username="other", password_hash="fixture")
        session.add_all([user, other]);session.commit()

    class Workflow:
        calls = 0
        failure = None
        entered = Event()
        release = Event()

        def run(self, context, history, summary, on_answer_event=None):
            self.calls += 1
            on_answer_event("agent_started", {"agent": "sql", "status": "running"})
            on_answer_event("answer_start", "")
            on_answer_event("answer_delta", "草稿")
            self.entered.set()
            assert self.release.wait(5), "fixture did not release workflow"
            if self.failure:
                raise self.failure
            on_answer_event("agent_finished", {"agent": "sql", "status": "ok"})
            return AgentRunResult(ModelAnswer("已校验的最终答案", "fixture", "fixture"), "smalltalk", [], "sql", {"search_mode": "sql", "metrics": {"context": {"calls": []}}})

    workflow = Workflow()
    sessions = SessionService(database)
    conversation = sessions.create(user.id, "replay fixture")
    chat = ChatService(settings, database, sessions, workflow)
    runs = RunService(settings, database, chat)
    value = SimpleNamespace(settings=settings, database=database, sessions=sessions, chat=chat, runs=runs,
                            store=runs.store, uid=user.id, other=other.id, sid=conversation.id, workflow=workflow)
    try:
        yield value
    finally:
        workflow.release.set();runs.close();engine.dispose()


def start(system, key=None):
    return system.runs.start(system.uid, system.sid, AgentChatInput("你好", "sql"), "fixture-request", key or uuid4())


def terminal(system, run):
    end = monotonic() + 5
    while monotonic() < end:
        result, rows = system.store.read(system.uid, run.id)
        if result.state != "running":
            return result, rows
        sleep(.02)
    pytest.fail("run did not terminate")


def test_events_persist_before_delivery_and_terminal_message_is_atomic(system):
    run = start(system)
    assert system.workflow.entered.wait(3)
    active, rows = system.store.read(system.uid, run.id)
    assert active.state == "running" and [row.event for row in rows] == ["run_started", "agent_started", "answer_start", "answer_delta"]
    assert rows[-1].payload["delta"] == "草稿"
    system.workflow.release.set()
    finished, rows = terminal(system, run)
    assert finished.state == "succeeded" and rows[-1].event == "done"
    assert [row.sequence for row in rows] == list(range(1, len(rows) + 1))
    assert rows[-1].payload["answer"] == "已校验的最终答案"
    _, messages = system.sessions.get(system.uid, system.sid)
    assert [message.role for message in messages] == ["user", "assistant"]
    assert str(messages[-1].id) == rows[-1].payload["message_id"] == str(finished.message_id)


def test_repeated_post_reuses_run_and_conflicting_payload_is_rejected(system):
    key = uuid4()
    run = start(system, key)
    assert start(system, key).id == run.id
    with pytest.raises(AppError) as failure:
        system.runs.start(system.uid, system.sid, AgentChatInput("不同问题", "sql"), "fixture", key)
    assert failure.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AppError) as failure:
        start(system)
    assert failure.value.code == "RESOURCE_BUSY"
    system.workflow.release.set();terminal(system, run)
    assert start(system, key).id == run.id and system.workflow.calls == 1


def test_empty_shared_scope_preserves_pre_sharing_idempotency_fingerprint(system):
    import hashlib
    import json
    from dataclasses import asdict
    key = uuid4()
    payload = AgentChatInput("你好", "sql")
    legacy = asdict(payload);legacy.pop("knowledge_base_ids")
    expected = hashlib.sha256(json.dumps({"session_id":str(system.sid),"input":legacy},sort_keys=True,default=str).encode()).hexdigest()
    run = start(system, key)
    with system.database.sessions() as session:
        assert session.get(ChatRun, run.id).fingerprint == expected
    assert start(system,key).id == run.id


def test_expiry_is_persisted_and_late_completion_cannot_save_assistant(system):
    run = start(system)
    assert system.workflow.entered.wait(3)
    with system.database.sessions() as session:
        session.get(ChatRun, run.id).expires_at = system.store.now(session) - timedelta(seconds=1)
        session.commit()
    interrupted, rows = system.store.read(system.uid, run.id)
    assert interrupted.state == "interrupted" and rows[-1].payload["code"] == "RUN_INTERRUPTED"
    system.workflow.release.set();system.runs.close()
    _, messages = system.sessions.get(system.uid, system.sid)
    assert [message.role for message in messages] == ["user"]
    again, replay = system.store.read(system.uid, run.id)
    assert again.sequence == interrupted.sequence and replay[-1].event == "error"


def test_terminal_events_failure_rolls_back_assistant_then_saves_error(system):
    run = start(system)
    assert system.workflow.entered.wait(3)
    original = system.store._event

    def fail_done(session, record, event, data, **kwargs):
        if event == "done":
            raise RuntimeError("private secret detail")
        return original(session, record, event, data, **kwargs)

    with patch.object(system.store, "_event", side_effect=fail_done):
        system.workflow.release.set()
        failed, rows = terminal(system, run)
    assert failed.state == "failed" and rows[-1].event == "error"
    assert "usage" not in [row.event for row in rows] and "context_usage" not in [row.event for row in rows]
    assert "private secret" not in str(rows[-1].payload)
    _, messages = system.sessions.get(system.uid, system.sid)
    assert [message.role for message in messages] == ["user"]


def test_capacity_is_bounded_but_idempotent_lookup_still_works(system):
    key = uuid4()
    run = start(system, key)
    assert system.workflow.entered.wait(3)
    assert system.runs.slots.acquire(blocking=False)
    try:
        assert start(system, key).id == run.id
        with pytest.raises(AppError) as failure:
            start(system)
        assert failure.value.code == "RUN_CAPACITY_EXCEEDED"
    finally:
        system.runs.slots.release()


def test_cross_user_cursor_and_cascade_boundaries(system):
    run = start(system)
    with pytest.raises(AppError) as failure:
        system.store.read(system.other, run.id)
    assert failure.value.status_code == 404
    with pytest.raises(AppError) as failure:
        system.store.read(system.uid, run.id, 999999)
    assert failure.value.code == "INVALID_EVENT_CURSOR"
    system.workflow.release.set();terminal(system, run)
    system.sessions.delete(system.uid, system.sid)
    with system.database.sessions() as session:
        assert session.get(ChatRun, run.id) is None
        assert session.scalar(select(ChatRunEvent).where(ChatRunEvent.run_id == run.id)) is None


def test_event_limit_preserves_room_for_error(system):
    run, _ = system.store.create(system.uid, system.sid, AgentChatInput("fixture", "sql"), "fixture", uuid4())
    with system.database.sessions() as session:
        session.get(ChatRun, run.id).sequence = 8192;session.commit()
    with pytest.raises(AppError) as failure:
        system.store.append(system.uid, run.id, "answer_delta", "x")
    assert failure.value.code == "RUN_EVENT_LIMIT"
    system.store.fail(system.uid, run.id, failure.value)
    state, _ = system.store.read(system.uid, run.id, 8192)
    assert state.state == "failed"


def test_http_replay_uses_cursor_and_does_not_run_again(system):
    services = Services()
    for name in ("settings", "database", "sessions", "chat", "runs"):
        setattr(services, name, getattr(system, name))
    services.current_user = system.uid
    headers = {"Authorization": "Bearer fixture", "Idempotency-Key": str(uuid4())}
    with TestClient(create_app(system.settings, services)) as client:
        response = client.post(f"/api/v2/sessions/{system.sid}/runs", headers=headers, json={"question": "你好", "search_mode": "sql"})
        assert response.status_code == 202
        rid = UUID(response.json()["run_id"])
        system.workflow.release.set()
        run, _ = system.store.read(system.uid, rid);terminal(system, run)
        replay = client.get(f"/api/v2/runs/{rid}/events", headers={**headers, "Last-Event-ID": f"{rid}:3"})
        assert replay.status_code == 200 and f"id: {rid}:4" in replay.text and "event: done" in replay.text
        assert "event: run_started" not in replay.text
        assert client.get(f"/api/v2/runs/{rid}/events?after=9999", headers=headers).status_code == 422
        assert client.get(f"/api/v2/runs/{rid}/events", headers={**headers,"Last-Event-ID":f"{uuid4()}:1"}).status_code == 422
        assert client.post(f"/api/v2/sessions/{system.sid}/runs", headers=headers, json={"question": "你好", "search_mode": "sql"}).json()["run_id"] == str(rid)
        legacy_url = f"/api/v2/sessions/{system.sid}/messages/stream"
        assert "event: done" in client.post(legacy_url, headers=headers, json={"question": "你好", "search_mode": "sql"}).text
        assert client.post(legacy_url, headers={**headers,"Idempotency-Key":"invalid"}, json={"question":"你好"}).status_code == 422
        finished, _ = system.store.read(system.uid, rid)
        assert client.get(f"/api/v2/runs/{rid}/events?after={finished.sequence}", headers=headers).text == ""
        services.current_user = system.other
        assert client.get(f"/api/v2/runs/{rid}", headers=headers).status_code == 404
        assert client.get(f"/api/v2/runs/{rid}/events", headers=headers).status_code == 404
        assert client.get(f"/api/v2/sessions/{system.sid}/runs", headers=headers).status_code == 404
    assert system.workflow.calls == 1


def test_real_supervisor_sql_citations_survive_event_replay(system):
    from app.workflows.supervisor import SupervisorWorkflow
    from test_agent_v2 import Models, ScriptedModel, SQL, Web, call, final, report

    model = ScriptedModel(responses=[call("delegate_sql", task="统计销量"), call("describe_sql_schema"),
                                    call("execute_sql", query="SELECT SUM(amount) FROM sales.orders"), report(), final("销量42[1]")])
    system.chat.agent_workflow = SupervisorWorkflow(system.settings, None, Models(model), Web(), sql=SQL())
    run = start(system)
    state, rows = terminal(system, run)
    assert state.state == "succeeded"
    assert any(row.event == "agent_started" for row in rows)
    done = rows[-1].payload
    assert done["citations"][0]["type"] == "sql" and done["answer"] == "销量42[1]"
    before = model.position
    second_reader = RunStore(system.settings, system.database)
    _, replay = second_reader.read(system.uid, run.id, 1)
    assert replay[-1].payload == done and model.position == before


def test_small_model_chunks_are_coalesced_without_losing_the_tail(system):
    def generate(context, history, summary, on_answer_event):
        on_answer_event("answer_start", "")
        for _ in range(1001):
            on_answer_event("answer_delta", "x")
        return AgentRunResult(ModelAnswer("x" * 1001, "fixture", "fixture"), "smalltalk", [], "sql", {"search_mode":"sql"})

    system.workflow.run = generate
    run = start(system)
    _, rows = terminal(system, run)
    deltas = [row.payload["delta"] for row in rows if row.event == "answer_delta"]
    assert "".join(deltas) == "x" * 1001 and len(deltas) < 10


def test_bootstrap_is_additive():
    from app.infrastructure.persistence.database import Database
    database = Database(Settings(_env_file=None))
    database.__dict__["engine"] = MagicMock()
    with patch("app.infrastructure.persistence.database.inspect") as inspector, patch.object(Base.metadata, "create_all") as create:
        inspector.return_value.has_table.return_value = True
        database.initialize_chat_runs()
        assert create.call_args.kwargs["tables"] == [ChatRun.__table__, ChatRunEvent.__table__]
