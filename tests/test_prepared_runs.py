"""Prepared execution keeps one message pair and commits control state atomically."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, AgentRunResult, ModelAnswer
from app.infrastructure.persistence.models import ChatMessage, ChatRun, ChatRunCheckpoint, User
from test_chat_runs import system


def prepared(s):
    return s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql"), "prepared", uuid4(),
                          queued=True, prepare=s.chat.prepare_run)[0]


def result(content="finished", kind="smalltalk"):
    return AgentRunResult(ModelAnswer(content, "fixture", "fixture"), kind, [], "sql", {"search_mode": "sql"})


def test_prepared_human_resume_updates_same_assistant_and_preserves_frozen_model(system):
    s = system
    run, seen = prepared(s), []
    original_model = run.execution_snapshot["settings"]["deepseek_model"]
    s.settings.deepseek_model = "changed-after-queue"

    def execute(context, history, summary, *, checkpoint, **kwargs):
        seen.append((context, checkpoint.state, history))
        if not checkpoint.state.get("human_inputs"):
            return result("请补充地区", "clarification")
        return result("境内结果")

    class Workflow:
        def run(self, *args, **kwargs):
            assert self.settings.deepseek_model == self.models.settings.deepseek_model == original_model
            return execute(*args, **kwargs)

    s.chat.agent_workflow = Workflow()
    first = s.chat.execute_prepared(s.store, s.store.claim_next())
    paused, events = s.store.read(s.uid, run.id)
    assert paused.state == "waiting" and first.id == run.message_id
    waiting = events[-1].payload
    s.store.resume(s.uid, run.id, waiting["checkpoint_revision"], paused.resume_key, "境内")
    final = s.chat.execute_prepared(s.store, s.store.claim_next())
    done, events = s.store.read(s.uid, run.id)
    assert done.state == "succeeded" and final.id == first.id and final.content == "境内结果"
    assert seen[1][1]["human_inputs"] == ["境内"] and seen[1][2] == []
    assert done.execution_snapshot["settings"]["deepseek_model"] == original_model
    with s.database.sessions() as database:
        messages = database.scalars(select(ChatMessage)).all()
        assert len(messages) == 2 and {message.id for message in messages} == {run.user_message_id, run.message_id}
    assert events[-1].payload["message_id"] == str(run.message_id)


def test_prepared_pause_failure_rolls_back_assistant_and_checkpoint(system, monkeypatch):
    s = system
    run = prepared(s)
    s.chat.agent_workflow = SimpleNamespace(run=lambda *args, **kwargs: result("请补充地区", "clarification"))
    original = s.store.pause

    def fail_after_pause(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("transaction interrupted")

    monkeypatch.setattr(s.store, "pause", fail_after_pause)
    with pytest.raises(RuntimeError, match="transaction interrupted"):
        s.chat.execute_prepared(s.store, s.store.claim_next())
    with s.database.sessions() as database:
        assert database.get(ChatMessage, run.message_id).content == ""
        assert database.get(ChatRun, run.id).state == "running"
        assert database.get(ChatRunCheckpoint, run.id) is None
    assert not any(event.event == "run_waiting" for event in s.store.read(s.uid, run.id)[1])


def test_prepared_rechecks_account_after_generation_before_publishing(system):
    s = system
    run = prepared(s)

    def revoke(*args, **kwargs):
        with s.database.sessions() as database:
            database.get(User, s.uid).is_active = False
            database.commit()
        return result()

    s.chat.agent_workflow = SimpleNamespace(run=revoke)
    with pytest.raises(AppError) as error:
        s.chat.execute_prepared(s.store, s.store.claim_next())
    assert error.value.code == "FORBIDDEN"
    with s.database.sessions() as database:
        assert database.get(ChatMessage, run.message_id).content == ""
    assert not any(event.event == "done" for event in s.store.read(s.uid, run.id)[1])


def test_worker_flushes_draft_before_terminal_and_cancellation_wins(system):
    from app.application.run_execution import RunWorker
    s = system
    run = prepared(s)

    def execute(*args, on_answer_event, **kwargs):
        on_answer_event("answer_start", "")
        on_answer_event("answer_delta", "first")
        on_answer_event("answer_delta", "last")
        return result()

    s.chat.agent_workflow = SimpleNamespace(run=execute)
    worker = RunWorker(s.store, s.chat)
    assert worker.run_once() and not worker.run_once()
    done, events = s.store.read(s.uid, run.id)
    assert done.state == "succeeded"
    assert "".join(event.payload["delta"] for event in events if event.event == "answer_delta") == "firstlast"
    assert events[-2].event == "usage" and events[-1].event == "done"

    # Another Run in the same session uses the original history; cancelling
    # during generation must leave its placeholder unpublished.
    second = prepared(s)

    def cancel(*args, **kwargs):
        s.store.request_cancel(s.uid, second.id)
        return result("must not publish")

    s.chat.agent_workflow = SimpleNamespace(run=cancel)
    assert worker.run_once()
    cancelled, events = s.store.read(s.uid, second.id)
    assert cancelled.state == "cancelled" and events[-1].event == "run_cancelled"
    with s.database.sessions() as database:
        assert database.get(ChatMessage, second.message_id).content == ""
        assert database.get(ChatMessage, second.message_id).run_metadata["state"] == "cancelled"


def test_real_graph_recovers_after_final_transaction_crash_without_repeating_calls(system, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.workflows.supervisor import SupervisorWorkflow
    from app.infrastructure.llms import providers
    from test_agent_v2 import Models, ScriptedModel, Web, call, final, report
    from test_run_workflow_checkpoints import CountSQL

    s = system
    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    monkeypatch.setattr(s.store, "now", lambda session: clock.now)
    run = prepared(s)
    sql = CountSQL()
    model = ScriptedModel(responses=[call("delegate_sql", task="汇总"), call("describe_sql_schema"),
        call("execute_sql", query="SELECT 1"), report(), final("42[1]")])
    monkeypatch.setattr(providers, "ModelRouter", lambda *args: Models(model))
    s.chat.agent_workflow = SupervisorWorkflow(s.settings, None, Models(model), Web(), sql=sql)
    original = s.store.complete

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated process loss before commit")

    monkeypatch.setattr(s.store, "complete", crash)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        s.chat.execute_prepared(s.store, s.store.claim_next())
    assert model.position == 5 and sql.queries == 1
    assert not any(event.event == "done" for event in s.store.read(s.uid, run.id)[1])
    clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    model = ScriptedModel(responses=[])
    monkeypatch.setattr(s.store, "complete", original)
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert message.id == run.message_id and message.content == "42[1]"
    assert sql.queries == 1 and model.position == 0
    assert message.run_metadata["model_attempts"] == 5
    with s.database.sessions() as database:
        assert len(database.scalars(select(ChatMessage)).all()) == 2


def test_http_queue_wait_resume_and_cancel_replay_keep_original_run(system):
    from fastapi.testclient import TestClient
    from app.application.run_execution import RunWorker
    from app.main import create_app
    from test_api_v2 import Services

    s = system
    services = Services()
    for name in ("settings", "database", "sessions", "chat", "runs"):
        setattr(services, name, getattr(s, name))
    services.current_user = s.uid
    headers = {"Authorization": "Bearer fixture", "Idempotency-Key": str(uuid4())}
    seen = []

    def execute(*args, checkpoint, **kwargs):
        seen.append(checkpoint.state)
        return result("请补充地区", "clarification") if not checkpoint.state.get("human_inputs") else result("境内结果")

    s.chat.agent_workflow = SimpleNamespace(run=execute)
    worker = RunWorker(s.store, s.chat)
    with TestClient(create_app(s.settings, services)) as client:
        response = client.post(f"/api/v2/sessions/{s.sid}/runs", headers=headers,
                               json={"question": "fixture", "search_mode": "sql"})
        assert response.status_code == 202 and response.json()["state"] == "queued" and not seen
        rid = response.json()["run_id"]
        assert worker.run_once()
        waiting = client.get(f"/api/v2/runs/{rid}", headers=headers).json()
        assert waiting["state"] == "waiting" and waiting["waiting"]["question"] == "请补充地区"
        assert "event: run_waiting" in client.get(waiting["events_url"], headers=headers).text
        body = {name: waiting["waiting"][name] for name in ("checkpoint_revision", "resume_token")}
        body["supplement"] = "境内"
        services.current_user = s.other
        assert client.post(f"/api/v2/runs/{rid}/resume", headers=headers, json=body).status_code == 404
        assert client.post(f"/api/v2/runs/{rid}/cancel", headers=headers).status_code == 404
        services.current_user = s.uid
        assert client.post(f"/api/v2/runs/{rid}/resume", headers=headers, json={**body, "supplement": " "}).status_code == 422
        for _ in range(2):
            resumed = client.post(f"/api/v2/runs/{rid}/resume", headers=headers, json=body)
            assert resumed.status_code == 202 and resumed.json()["run_id"] == rid
        assert client.post(f"/api/v2/runs/{rid}/resume", headers=headers, json={**body, "supplement": "境外"}).status_code == 409
        assert worker.run_once()
        done = client.get(f"/api/v2/runs/{rid}", headers=headers).json()
        assert done["state"] == "succeeded" and done["message_id"] == waiting["message_id"]
        assert client.post(f"/api/v2/runs/{rid}/cancel", headers=headers).json()["state"] == "succeeded"
        queued = client.post(f"/api/v2/sessions/{s.sid}/runs", headers={**headers, "Idempotency-Key": str(uuid4())},
                             json={"question": "cancel before execution", "search_mode": "sql"}).json()
        assert client.post(f"/api/v2/runs/{queued['run_id']}/cancel", headers=headers).json()["state"] == "cancelled"
        assert not worker.run_once() and len(seen) == 2
        assert "event: run_cancelled" in client.get(queued["events_url"], headers=headers).text
