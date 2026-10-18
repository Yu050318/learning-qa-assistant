from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domain.model_catalog import MODEL_ROLES, ModelCatalog
from test_model_center import center
from test_chat_runs import system


def configuration():
    answer = dict(id="answer-model", version=1, display_name="回答模型", provider="deepseek", connection_id="deepseek",
        model_name="answer-model", context_window=32768, max_output_tokens=4096, timeout_seconds=30,
        supports_stream=True, supports_tools=True, supports_structured_output=True, enabled=True)
    fast = {**answer, "id": "fast-model", "display_name": "快速规划", "model_name": "fast-model", "supports_stream": False, "supports_tools": False}
    grader = {**fast, "id": "grader-model", "display_name": "证据判断", "model_name": "grader-model"}
    evaluator = {**fast, "id": "evaluator-model", "display_name": "质量评测", "model_name": "evaluator-model"}
    return {"profiles": {profile["id"]: profile for profile in (answer, fast, grader, evaluator)},
            "assignments": {role: role + "-model" for role in MODEL_ROLES}, "provider_defaults": {"deepseek": "answer-model"}}


def test_four_roles_can_use_four_models_from_one_connection_without_two_fixed_slots():
    catalog = ModelCatalog.model_validate(configuration())
    assert [catalog.require(role).model_name for role in MODEL_ROLES] == [role + "-model" for role in MODEL_ROLES]
    assert len(catalog.profiles) == 4 and catalog.profiles["fast-model"].provider == "deepseek"
    assert "api_key" not in catalog.model_dump_json() and "base_url" not in catalog.model_dump_json()


@pytest.mark.parametrize("change", ["missing_role", "missing_profile", "disabled", "stream", "tools", "structured",
                                   "identity", "wrong_provider", "budget", "client_secret", "client_endpoint"])
def test_catalog_rejects_unusable_roles_or_untrusted_connection_material(change):
    data = configuration()
    if change == "missing_role":
        del data["assignments"]["evaluator"]
    elif change == "missing_profile":
        del data["profiles"]["grader-model"]
    elif change == "disabled":
        data["profiles"]["fast-model"]["enabled"] = False
    elif change in {"stream", "tools"}:
        data["profiles"]["answer-model"]["supports_" + change] = False
    elif change == "structured":
        data["profiles"]["evaluator-model"]["supports_structured_output"] = False
    elif change == "identity":
        data["profiles"]["grader-model"]["id"] = "different-identity"
    elif change == "wrong_provider":
        data["provider_defaults"] = {"ollama": "answer-model"}
    elif change == "budget":
        data["profiles"]["fast-model"]["context_window"] = 4096
    else:
        data["profiles"]["answer-model"]["api_key" if change == "client_secret" else "base_url"] = "not-accepted"
    with pytest.raises(ValidationError):
        ModelCatalog.model_validate(data)


def test_fallback_is_bounded_and_must_support_the_assigned_role():
    data = configuration()
    data["profiles"]["answer-model"]["fallback_profile_id"] = "fast-model"
    with pytest.raises(ValidationError, match="Fallback does not support"):
        ModelCatalog.model_validate(data)
    fallback = deepcopy(data["profiles"]["answer-model"])
    fallback.update(id="answer-fallback", fallback_profile_id=None, provider="ollama", connection_id="ollama")
    data["profiles"]["answer-fallback"] = fallback
    data["profiles"]["answer-model"]["fallback_profile_id"] = "answer-fallback"
    assert ModelCatalog.model_validate(data).require("answer").fallback_profile_id == "answer-fallback"
    data["profiles"]["answer-fallback"]["fallback_profile_id"] = "answer-model"
    with pytest.raises(ValidationError, match="no further fallback"):
        ModelCatalog.model_validate(data)


def test_catalog_crud_assignments_history_and_profile_versions_are_atomic(center):
    from app.core.errors import AppError
    s = center
    initial = s.center.catalog_view(s.uid)
    assert initial["revision"] == 1 and set(initial["catalog"]["profiles"]) == {"deepseek", "ollama"}
    catalog = ModelCatalog.model_validate(configuration())
    with pytest.raises(AppError) as error:
        s.center.update_catalog(s.other, 1, catalog)
    assert error.value.code == "FORBIDDEN"
    saved = s.center.update_catalog(s.uid, 1, catalog)
    assert saved["revision"] == 2 and len(saved["catalog"]["profiles"]) == 4
    frozen = s.center.snapshot()
    assert frozen.catalog.require("grader").id == "grader-model"
    assert frozen.router().for_role("fast").profile.model_name == "fast-model"
    assert s.center.snapshot("deepseek").settings.deepseek_model == "answer-model"
    changed = deepcopy(saved["catalog"])
    changed["profiles"]["fast-model"]["model_name"] = "updated-fast-model"
    updated = s.center.update_catalog(s.uid, 2, ModelCatalog.model_validate(changed))
    assert updated["catalog"]["profiles"]["fast-model"]["version"] == 2
    assert updated["catalog"]["profiles"]["answer-model"]["version"] == 1
    assert frozen.catalog.require("fast").model_name == "fast-model"
    changed = deepcopy(updated["catalog"])
    changed["assignments"]["fast"] = "answer-model"
    del changed["profiles"]["fast-model"]
    removed = s.center.update_catalog(s.uid, 3, ModelCatalog.model_validate(changed))
    assert "fast-model" not in removed["catalog"]["profiles"]
    history = s.center.history(s.uid)
    assert history["total"] == 4 and history["items"][1]["configuration"]["profiles"]["fast-model"]["version"] == 2
    with pytest.raises(AppError) as error:
        s.center.update(s.uid, 4, frozen.configuration)
    assert error.value.code == "MODEL_CATALOG_REQUIRED"
    assert s.center.history(s.uid)["total"] == 4
    reuse = deepcopy(removed["catalog"])
    reuse["profiles"]["fast-model"] = configuration()["profiles"]["fast-model"]
    with pytest.raises(AppError) as error:
        s.center.update_catalog(s.uid, 4, ModelCatalog.model_validate(reuse))
    assert error.value.code == "MODEL_PROFILE_RETIRED" and s.center.history(s.uid)["total"] == 4


def test_catalog_cannot_rebind_credentials_or_forge_profile_versions(center):
    from app.core.errors import AppError
    s = center
    data = configuration()
    data["profiles"]["answer-model"]["connection_id"] = "attacker-endpoint"
    with pytest.raises(AppError) as error:
        s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(data))
    assert error.value.code == "MODEL_CONNECTION_UNAVAILABLE"
    assert s.center.history(s.uid)["total"] == 1
    data = configuration()
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(data))
    data["profiles"]["answer-model"]["version"] = 999
    with pytest.raises(AppError) as error:
        s.center.update_catalog(s.uid, 2, ModelCatalog.model_validate(data))
    assert error.value.code == "MODEL_PROFILE_VERSION_CHANGED"
    assert s.center.history(s.uid)["total"] == 2


def test_profile_router_applies_role_limits_and_same_provider_fallback(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from app.core.config import Settings
    from app.core.errors import UpstreamError
    from app.infrastructure.llms.catalog import CatalogRouter
    from app.infrastructure.llms.providers import DeepSeekProvider
    from app.workflows.agent import ModelRuntime
    from test_agent_v2 import ScriptedModel
    from time import monotonic

    data = configuration()
    data["profiles"]["answer-backup"] = {**data["profiles"]["answer-model"], "id": "answer-backup", "model_name": "backup-model"}
    data["profiles"]["answer-model"]["fallback_profile_id"] = "answer-backup"
    data["profiles"]["fast-model"].update(timeout_seconds=5, max_output_tokens=2000)
    models = {"answer-model": ScriptedModel(responses=[UpstreamError("llm", retryable=True)]),
              "backup-model": ScriptedModel(responses=[AIMessage(content="备用回答")])}
    clients = []

    def client(implementation, timeout):
        clients.append((implementation.model_name, timeout, implementation.settings.model_max_tokens))
        return models.get(implementation.model_name, ScriptedModel(responses=[]))

    monkeypatch.setattr(DeepSeekProvider, "agent_client", client)
    settings = Settings(_env_file=None, deepseek_api_key="fixture")
    router = CatalogRouter(settings, ModelCatalog.model_validate(data))
    fast = router.for_role("fast")
    assert fast.agent_model(None, 90)[2] == "fast-model" and clients[-1] == ("fast-model", 5, 2000)
    primary, provider, name = router.agent_model(None, 30)
    runtime = ModelRuntime(router, primary, provider, name, router.agent_fallback(provider, 30),
                           4, monotonic() + 60, 30, router.input_budget)
    answer = runtime.invoke([HumanMessage(content="fixture")])
    assert answer.content == "备用回答" and runtime.active_provider == "deepseek" and runtime.active_model_name == "backup-model"
    assert runtime.attempts == 2 and models["answer-model"].position == models["backup-model"].position == 1


def test_small_planner_and_evaluator_windows_do_not_shrink_the_answer_budget(center):
    s = center
    s.settings.agent_context_token_budget = 20_000
    data = configuration()
    data["profiles"]["fast-model"].update(context_window=4096, max_output_tokens=2000)
    data["profiles"]["evaluator-model"].update(context_window=4096, max_output_tokens=3000)
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(data))
    snapshot = s.center.snapshot()
    router = snapshot.router()
    assert snapshot.settings.agent_context_token_budget == router.input_budget == 20_000
    assert router.for_role("fast").input_budget == 2096
    assert router.for_role("evaluator").input_budget == 1096
