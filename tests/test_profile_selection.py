from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.api.schemas import CompactRequest, ContextEstimateRequest, V2ChatRequest
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.domain.model_catalog import ModelCatalog
from app.infrastructure.llms.providers import DeepSeekProvider, OllamaProvider
from app.infrastructure.persistence.models import DocumentRecord, ModelCall
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import ScriptedModel, Web, call, final
from test_knowledge_quality import Retriever, grade, plan
from test_model_catalog import configuration
from test_model_center import center
from test_chat_runs import system


@pytest.mark.parametrize("schema", [V2ChatRequest, ContextEstimateRequest, CompactRequest])
def test_provider_and_profile_are_mutually_exclusive_at_every_boundary(schema):
    common = {"question": "fixture"} if schema is not CompactRequest else {"expected_context_version": "fixture"}
    with pytest.raises(ValidationError):
        schema.model_validate({**common, "model_provider": "deepseek", "model_profile_id": "answer-model"})
    with pytest.raises(ValidationError):
        schema.model_validate({**common, "model_profile_id": "https://untrusted.example/model"})


def test_selected_answer_profile_keeps_other_roles_frozen_and_is_recorded_in_ledger(center, monkeypatch):
    s = center
    s.settings.knowledge_quality_enabled = True
    s.chat.model_center = s.center
    data = configuration()
    data["profiles"]["alternate-answer"] = {**data["profiles"]["answer-model"], "id": "alternate-answer",
        "provider": "ollama", "connection_id": "ollama", "model_name": "alternate-model"}
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(data))
    models = {"alternate-model": ScriptedModel(responses=[call("delegate_rag", task="NX-1 功率"), final("功率100W[1]")]),
              "fast-model": ScriptedModel(responses=[plan()]), "grader-model": ScriptedModel(responses=[grade()])}
    names = []

    def client(implementation, timeout):
        names.append(implementation.model_name)
        return models[implementation.model_name]

    monkeypatch.setattr(DeepSeekProvider, "agent_client", client)
    monkeypatch.setattr(OllamaProvider, "agent_client", client)
    did = uuid4()
    with s.database.sessions() as session:
        session.add(DocumentRecord(id=did, user_id=s.uid, original_name="fixture.md", file_type=".md", file_path="fixture",
            content_hash="0" * 64, status="ready", embedding_model=s.settings.embedding_model, chunk_count=1))
        session.commit()
    s.chat.agent_workflow = SupervisorWorkflow(s.settings, Retriever(), None, Web())
    payload = AgentChatInput("功率？", "knowledge", document_ids=(did,), model_profile_id="alternate-answer")
    run, _ = s.store.create(s.uid, s.sid, payload, "explicit-profile", uuid4(), queued=True, prepare=s.chat.prepare_run)
    assert run.execution_snapshot["model_center"]["answer_profile_id"] == "alternate-answer"
    assert run.execution_snapshot["model_center"]["catalog"]["assignments"]["answer"] == "answer-model"
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert message.model_provider == "ollama" and message.model_name == "alternate-model"
    assert set(names) == set(models) and [model.position for model in models.values()] == [2, 1, 1]
    assert message.run_metadata["model_roles"]["answer"]["profile_id"] == "alternate-answer"
    assert message.run_metadata["model_roles"]["fast"]["profile_id"] == "fast-model"
    with s.database.sessions() as session:
        calls = session.scalars(select(ModelCall).order_by(ModelCall.attempt_index)).all()
        assert [(row.model_role, row.model_profile_id, row.model_profile_version) for row in calls] == [
            ("answer", "alternate-answer", 1), ("fast", "fast-model", 1), ("grader", "grader-model", 1), ("answer", "alternate-answer", 1)]
    statistics = s.center.statistics(s.uid)
    assert sum(row["attempts"] for row in statistics["models"]) == 4
    assert {row["model_role"] for row in statistics["models"]} == {"answer", "fast", "grader"}


def test_profile_probe_checks_exact_model_and_rejects_stale_result(center):
    s = center
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(configuration()))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "fast-model"}]})

    report = s.center.probe_profile(s.uid, "fast-model", transport=httpx.MockTransport(respond))
    states = {model["profile_id"]: model["health"] for model in report["models"]}
    assert states["fast-model"] == "available" and states["answer-model"] == "unknown"
    assert requests[0].method == "GET" and requests[0].url.path == "/models"
    assert "fixture-not-real-key" not in str(report)
    with pytest.raises(AppError) as error:
        s.center.probe_profile(s.other, "fast-model", transport=httpx.MockTransport(respond))
    assert error.value.code == "FORBIDDEN" and len(requests) == 1

    def change_during_probe(request):
        data = report["catalog"]
        data["profiles"]["fast-model"]["model_name"] = "changed-fast-model"
        s.center.update_catalog(s.uid, 2, ModelCatalog.model_validate(data))
        return httpx.Response(200, json={"data": [{"id": "fast-model"}]})

    with pytest.raises(AppError) as error:
        s.center.probe_profile(s.uid, "fast-model", transport=httpx.MockTransport(change_during_probe))
    assert error.value.code == "MODEL_CONFIGURATION_CHANGED"
    assert all(model["health"] == "unknown" for model in s.center.catalog_view(s.uid)["models"])


def test_capabilities_offer_only_answer_capable_profiles_and_estimator_uses_selection(center):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api_v2 import Services
    s = center
    s.chat.model_center = s.center
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(configuration()))
    services = Services()
    services.settings, services.model_center, services.current_user = s.settings, s.center, s.uid
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(create_app(s.settings, services)) as client:
        cap = client.get("/api/v2/capabilities", headers=headers).json()
        assert cap["default_model_profile_id"] == "answer-model" and cap["features"]["model_profiles"]
        assert [model["profile_id"] for model in cap["models"] if model["available"]] == ["answer-model"]
        assert client.get("/api/v2/model-center/catalog", headers=headers).status_code == 200
    estimate = s.chat.estimate_context(s.uid, ContextEstimateRequest(question="fixture", model_profile_id="answer-model"))
    assert estimate["model_name"] == "answer-model"
    with pytest.raises(AppError) as error:
        s.center.snapshot(profile_id="grader-model")
    assert error.value.code == "MODEL_DISABLED"


def test_evaluator_role_uses_its_profile_and_keeps_a_distinct_usage_record(center, monkeypatch):
    from app.domain.contracts import ChatTurn, ModelAnswer
    s = center
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(configuration()))
    names = []

    def generate(implementation, messages):
        names.append(implementation.model_name)
        return ModelAnswer('{"score":1}', "deepseek", implementation.model_name,
                           {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})

    monkeypatch.setattr(DeepSeekProvider, "generate", generate)
    snapshot = s.center.snapshot()
    answer = snapshot.router().for_role("evaluator").generate([ChatTurn("user", "isolated evaluation fixture")],
        call_observer=s.center.observer(s.uid, s.sid, "evaluation-profile", snapshot.revision, "evaluation"))
    assert names == ["evaluator-model"] and answer.model == "evaluator-model"
    with s.database.sessions() as session:
        row = session.scalar(select(ModelCall))
        assert (row.model_role, row.model_profile_id, row.model_profile_version) == ("evaluator", "evaluator-model", 1)
        assert row.status == "succeeded" and row.total_tokens == 3


def test_profile_changes_are_part_of_run_idempotency_and_disabled_selection_creates_no_messages(center):
    from sqlalchemy import func
    from app.infrastructure.persistence.models import ChatMessage, ChatRun
    s = center
    s.chat.model_center = s.center
    data = configuration()
    data["profiles"]["second-answer"] = {**data["profiles"]["answer-model"], "id": "second-answer"}
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(data))
    with pytest.raises(AppError):
        s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql", model_profile_id="grader-model"), "invalid", uuid4(),
                       queued=True, prepare=s.chat.prepare_run)
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 0
        assert session.scalar(select(func.count()).select_from(ChatRun)) == 0
    key = uuid4()
    first, _ = s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql", model_profile_id="answer-model"), "first", key,
                              queued=True, prepare=s.chat.prepare_run)
    assert s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql", model_profile_id="answer-model"), "retry", key,
                          queued=True, prepare=s.chat.prepare_run)[0].id == first.id
    with pytest.raises(AppError) as error:
        s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql", model_profile_id="second-answer"), "changed", key,
                       queued=True, prepare=s.chat.prepare_run)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
