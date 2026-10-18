from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.application.chat import ChatService
from app.application.model_center import ManagedModels, ModelCenter
from app.core.errors import AppError, UpstreamError
from app.domain.contracts import AgentChatInput, ModelAnswer
from app.infrastructure.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
from app.infrastructure.persistence.models import ModelCall, ModelConfiguration, ModelConfigurationVersion, User
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report, SQL
from test_chat_runs import system


@pytest.fixture
def center(system):
    s = system
    s.settings.model_center_enabled = True
    with s.database.sessions() as session:
        session.get(User, s.uid).role = "admin"
        session.commit()
    s.settings.deepseek_api_key = SecretStr("fixture-not-real-key")
    s.center = ModelCenter(s.settings, s.database)
    s.center.initialize()
    return s


def deny(function, *args, code=None, **kwargs):
    with pytest.raises(AppError) as result:
        function(*args, **kwargs)
    if code:
        assert result.value.code == code
    return result.value


def test_queued_run_freezes_managed_configuration_in_its_creation_transaction(center):
    s = center
    s.chat.model_center = s.center
    key = uuid4()
    run, created = s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql"), "queued-model-snapshot", key,
                                 queued=True, prepare=s.chat.prepare_run)
    assert created and run.execution_snapshot["model_center"]["revision"] == 1
    previous = run.execution_snapshot["model_center"]["configuration"]["profiles"]["deepseek"]["model_name"]
    config = s.center.snapshot().configuration.model_copy(deep=True)
    config.profiles["deepseek"].model_name = "changed-after-queue"
    s.center.update(s.uid, 1, config)
    again, created = s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql"), "same-request", key,
                                   queued=True, prepare=s.chat.prepare_run)
    assert not created and again.message_id == run.message_id and again.user_message_id == run.user_message_id
    assert again.execution_snapshot["model_center"]["revision"] == 1
    assert again.execution_snapshot["model_center"]["configuration"]["profiles"]["deepseek"]["model_name"] == previous
    assert "fixture-not-real-key" not in str(again.execution_snapshot)


def test_configuration_is_versioned_admin_only_and_old_snapshots_stay_frozen(center):
    s = center
    snapshot = s.center.snapshot()
    config = snapshot.configuration.model_copy(deep=True)
    config.profiles["deepseek"].model_name = "new-approved-model"
    deny(s.center.update, s.other, 1, config, code="FORBIDDEN")
    updated = s.center.update(s.uid, 1, config)
    assert updated["revision"] == 2 and s.center.snapshot().settings.deepseek_model == "new-approved-model"
    assert snapshot.settings.deepseek_model == "deepseek-chat"
    deny(s.center.update, s.uid, 1, config, code="MODEL_CONFIGURATION_CHANGED")
    assert "fixture-not-real-key" not in str(updated) and "base_url" not in str(updated)
    assert not s.center.view(s.other)["can_manage"]
    s.center.initialize()
    assert s.center.snapshot().revision == 2


def test_configuration_history_is_atomic_persistent_and_admin_only(center):
    s = center
    first = s.center.snapshot()
    changed = first.configuration.model_copy(deep=True)
    changed.profiles["deepseek"].model_name = "historical-second-model"
    s.center.update(s.uid, first.revision, changed)
    history = s.center.history(s.uid)
    assert history["total"] == 2 and [item["revision"] for item in history["items"]] == [2, 1]
    assert history["items"][1]["configuration"] == first.configuration.model_dump(mode="json")
    assert history["items"][0]["created_by"] == s.uid
    assert "fixture-not-real-key" not in str(history)
    deny(s.center.history, s.other, code="FORBIDDEN")
    deny(s.center.update, s.uid, first.revision, changed, code="MODEL_CONFIGURATION_CHANGED")
    assert s.center.history(s.uid)["total"] == 2
    with s.database.sessions() as session:
        assert session.get(ModelConfigurationVersion, 1).configuration["profiles"]["deepseek"]["model_name"] == "deepseek-chat"


@pytest.mark.parametrize("change", ["disable_default", "fallback_disabled", "context", "credentials", "unknown_field"])
def test_invalid_configuration_and_client_credentials_are_rejected(center, change):
    s = center
    data = s.center.snapshot().configuration.model_dump()
    if change == "disable_default":
        data["profiles"]["deepseek"]["enabled"] = False
    elif change == "fallback_disabled":
        data["allow_fallback"] = True;data["profiles"]["ollama"]["enabled"] = False
    elif change == "context":
        data["profiles"]["ollama"]["context_window"] = 4096;data["max_output_tokens"] = 8192
    else:
        data["api_key" if change == "credentials" else "endpoint_url"] = "not-accepted"
    with pytest.raises(ValidationError):
        ManagedModels.model_validate(data)


def test_probe_is_explicit_non_generation_and_does_not_leak_errors(center):
    s = center
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/models"
        return httpx.Response(200, json={"data":[{"id":"deepseek-chat"}]})

    deny(s.center.probe, s.other, "deepseek", code="FORBIDDEN", transport=httpx.MockTransport(respond))
    assert calls == [] and s.center.view(s.uid)["models"][0]["health"] == "unknown"
    view = s.center.probe(s.uid,"deepseek",transport=httpx.MockTransport(respond))
    assert view["models"][0]["health"] == "available" and len(calls) == 1
    bad = s.center.probe(s.uid,"deepseek",transport=httpx.MockTransport(lambda request:httpx.Response(401,text="private upstream secret")))
    assert bad["models"][0]["health"] == "unavailable" and "private upstream" not in str(bad)
    s.center.update(s.uid,1,s.center.snapshot().configuration)
    assert s.center.view(s.uid)["models"][0]["health"] == "unknown"


def test_provider_disable_and_fallback_share_the_existing_router_rules(center):
    s = center
    config = s.center.snapshot().configuration.model_copy(deep=True)
    config.profiles["deepseek"].enabled = False
    config.default_provider = "ollama"
    s.center.update(s.uid,1,config)
    snapshot = s.center.snapshot()
    deny(snapshot.router().agent_model,"deepseek",1,code="MODEL_DISABLED")
    assert snapshot.settings.default_model_provider == "ollama"


def test_context_budget_depends_on_selected_model_and_possible_fallback(center):
    s = center
    config = s.center.snapshot().configuration.model_copy(deep=True)
    config.profiles["ollama"].context_window = 4096
    s.center.update(s.uid,1,config)
    assert s.center.snapshot("deepseek").settings.agent_context_token_budget == 24000
    assert s.center.snapshot("ollama").settings.agent_context_token_budget == 2048
    config.allow_fallback = True
    s.center.update(s.uid,2,config)
    assert s.center.snapshot("deepseek").settings.agent_context_token_budget == 2048


def test_success_failed_and_incomplete_metrics_are_user_scoped_and_not_zero_filled(center):
    s = center
    observer = s.center.observer(s.uid,s.sid,"metrics-fixture",1)
    successful = observer.start("deepseek","fixture",1)
    observer.finish(successful,AIMessage(content="ok",usage_metadata={"input_tokens":4,"output_tokens":2,"total_tokens":6}))
    failed = observer.start("deepseek","fixture",2,True)
    observer.finish(failed,error=UpstreamError("llm",retryable=True))
    observer.start("deepseek","fixture",3)
    other_session = s.sessions.create(s.other,"private metrics")
    other = s.center.observer(s.other,other_session.id,"other-fixture",1)
    other.finish(other.start("ollama","other",1),ModelAnswer("ok","ollama","other"))
    row = s.center.statistics(s.uid)["models"][0]
    assert (row["attempts"],row["succeeded"],row["failed"],row["incomplete"]) == (3,1,1,1)
    assert row["total_tokens"] is None and row["reported_total_tokens"] == 6 and row["reported_calls"] == 1
    assert len(s.center.statistics(s.other)["models"]) == 1
    deny(s.center.statistics,s.other,30,True,code="FORBIDDEN")
    assert len(s.center.statistics(s.uid,30,True)["models"]) == 2
    s.sessions.delete(s.uid,s.sid)
    assert s.center.statistics(s.uid)["models"] == []


def test_direct_compaction_router_records_fallback_and_stops_on_metric_failure(center):
    s = center
    first = SimpleNamespace(name="deepseek",model_name="primary",generate=MagicMock(side_effect=UpstreamError("llm",retryable=True)))
    second = SimpleNamespace(name="ollama",model_name="fallback",generate=MagicMock(return_value=ModelAnswer("ok","ollama","fallback")))
    settings = s.settings.model_copy(update={"allow_model_fallback":True})
    router = ModelRouter(settings,{"deepseek":first,"ollama":second})
    observer = s.center.observer(s.uid,s.sid,"compact-fixture",1,"compaction")
    assert router.generate([],call_observer=observer).content == "ok"
    with s.database.sessions() as session:
        rows = session.scalars(select(ModelCall).where(ModelCall.operation == "compaction").order_by(ModelCall.attempt_index)).all()
    assert [(row.status,row.fallback) for row in rows] == [("failed",False),("succeeded",True)]
    before = first.generate.call_count
    with patch.object(observer,"start",side_effect=AppError("MODEL_METRICS_UNAVAILABLE","fixture",503)):
        deny(router.generate,[],call_observer=observer,code="MODEL_METRICS_UNAVAILABLE")
    assert first.generate.call_count == before
    first.generate.side_effect = None
    first.generate.return_value = ModelAnswer("ok","deepseek","primary")
    fallback_before = second.generate.call_count
    fresh_observer = s.center.observer(s.uid,s.sid,"metric-finish-failure",1)
    with patch.object(fresh_observer,"finish",side_effect=AppError("MODEL_METRICS_UNAVAILABLE","fixture",503)):
        deny(router.generate,[],call_observer=fresh_observer,code="MODEL_METRICS_UNAVAILABLE")
    assert first.generate.call_count == before + 1
    assert second.generate.call_count == fallback_before


def test_supervisor_records_all_attempts_including_failed_primary(center):
    s = center
    config = s.center.snapshot().configuration.model_copy(deep=True)
    config.allow_fallback = True
    s.center.update(s.uid,1,config)
    primary = ScriptedModel(responses=[UpstreamError("llm",retryable=True)])
    fallback = ScriptedModel(responses=[final("你好","smalltalk")])
    with patch.object(DeepSeekProvider,"agent_client",return_value=primary), patch.object(OllamaProvider,"agent_client",return_value=fallback):
        workflow = SupervisorWorkflow(s.settings,None,Models(primary),Web())
        chat = ChatService(s.settings,s.database,s.sessions,workflow)
        result = chat.answer_v2(s.uid,s.sid,AgentChatInput("你好","sql"),"fallback-metric")
    assert result.model_provider == "ollama" and result.run_metadata["model_configuration_revision"] == 2
    with s.database.sessions() as session:
        rows = session.scalars(select(ModelCall).order_by(ModelCall.attempt_index)).all()
    assert [(row.provider,row.status) for row in rows] == [("deepseek","failed"),("ollama","succeeded")]
    assert all(row.configuration_revision == 2 for row in rows)


def test_live_configuration_change_does_not_change_a_running_supervisor(center):
    s = center
    model = ScriptedModel(responses=[call("delegate_sql",task="统计销量"),call("describe_sql_schema"),call("execute_sql",query="SELECT SUM(amount) FROM sales.orders"),report(),final("42[1]")])
    names = []

    def client(implementation, timeout):
        names.append(implementation.model_name)
        if len(names) == 2:
            config = s.center.snapshot().configuration.model_copy(deep=True)
            config.profiles["deepseek"].model_name = "changed-after-start"
            s.center.update(s.uid,1,config)
        return model

    with patch.object(DeepSeekProvider,"agent_client",client):
        workflow = SupervisorWorkflow(s.settings,None,Models(model),Web(),sql=SQL())
        result = ChatService(s.settings,s.database,s.sessions,workflow).answer_v2(s.uid,s.sid,AgentChatInput("销量","sql"),"frozen-model")
    assert set(names) == {"deepseek-chat"} and result.run_metadata["model_configuration_revision"] == 1
    assert s.center.snapshot().settings.deepseek_model == "changed-after-start"


def test_http_configuration_and_stats_never_expose_credentials(center):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api_v2 import Services
    s = center
    services = Services();services.settings = s.settings;services.model_center = s.center;services.current_user = s.uid
    headers = {"Authorization":"Bearer fixture"}
    with TestClient(create_app(s.settings,services)) as client:
        current = client.get("/api/v2/model-center",headers=headers)
        assert current.status_code == 200 and "fixture-not-real-key" not in current.text
        data = {"expected_revision":1,"configuration":current.json()["configuration"]}
        services.current_user = s.other
        assert client.put("/api/v2/model-center",headers=headers,json=data).status_code == 403
        assert client.get("/api/v2/model-center/statistics?global_scope=true",headers=headers).status_code == 403
        assert client.post("/api/v2/model-center/probe/ollama",headers=headers).status_code == 403
        assert client.get("/api/v2/model-center/versions",headers=headers).status_code == 403
        assert client.get("/api/v2/capabilities",headers=headers).json()["features"]["model_center"] is True
        services.current_user = s.uid
        assert client.put("/api/v2/model-center",headers=headers,json=data).status_code == 200
        assert client.put("/api/v2/model-center",headers=headers,json=data).status_code == 409
        history = client.get("/api/v2/model-center/versions?limit=1",headers=headers)
        assert history.status_code == 200 and history.headers["Cache-Control"] == "no-store"
        assert history.json()["total"] == 2 and len(history.json()["items"]) == 1
        assert history.json()["items"][0]["revision"] == 2 and "fixture-not-real-key" not in history.text
