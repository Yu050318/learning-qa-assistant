from dataclasses import replace
from datetime import timedelta

import pytest

from app.application.run_checkpoint import RunCheckpoint
from app.core.errors import AppError
from app.domain.model_catalog import ModelCatalog
from app.infrastructure.llms.catalog import CatalogRouter
from app.infrastructure.llms.providers import DeepSeekProvider
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import ScriptedModel, Web, call, final
from test_knowledge_quality import Retriever, grade, plan, setup
from test_model_catalog import configuration
from test_model_center import center
from test_run_control import control, system


def role_models(monkeypatch, settings):
    models = {"answer-model": ScriptedModel(responses=[call("delegate_rag", task="NX-1 功率"), final("功率100W[1]")]),
              "fast-model": ScriptedModel(responses=[plan()]), "grader-model": ScriptedModel(responses=[grade()]),
              "evaluator-model": ScriptedModel(responses=[])}
    monkeypatch.setattr(DeepSeekProvider, "agent_client", lambda self, timeout: models[self.model_name])
    return CatalogRouter(settings, ModelCatalog.model_validate(configuration())), models


def test_actual_graph_routes_answer_planner_grader_and_recovery_reuses_each_role(control, monkeypatch):
    s = control
    s.settings.knowledge_quality_enabled = True
    router, models = role_models(monkeypatch, s.settings)
    _, ctx, _, retriever = setup([])
    ctx = replace(ctx, user_id=s.uid, session_id=s.sid)
    result = SupervisorWorkflow(s.settings, retriever, router, Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert result.answer.model == "answer-model" and result.run_metadata["model_attempts"] == 4
    assert [models[name].position for name in models] == [2, 1, 1, 0]
    assert set(result.run_metadata["usage_by_model"]) == {"deepseek/answer-model", "deepseek/fast-model", "deepseek/grader-model"}
    assert [item["attempt_index"] for item in result.run_metadata["metrics"]["context"]["calls"]] == [1, 2, 3, 4]
    s.clock.now += timedelta(seconds=s.settings.chat_run_lease_seconds + 1)
    # All scripts are exhausted: any accidental inference or new retrieval on
    # takeover would fail or increment the externally visible retrieval count.
    repeated = SupervisorWorkflow(s.settings, retriever, router, Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert repeated.answer.content == result.answer.content and repeated.citations == result.citations
    assert repeated.run_metadata["model_attempts"] == 4 and len(retriever.calls) == 1
    assert [models[name].position for name in models] == [2, 1, 1, 0]


def test_changing_roles_cannot_reset_the_shared_model_call_limit(control, monkeypatch):
    s = control
    s.settings.knowledge_quality_enabled = True
    s.settings.agent_max_model_calls = 3
    router, models = role_models(monkeypatch, s.settings)
    _, ctx, _, retriever = setup([])
    ctx = replace(ctx, user_id=s.uid, session_id=s.sid)
    with pytest.raises(AppError) as error:
        SupervisorWorkflow(s.settings, retriever, router, Web()).run(
            ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert error.value.code == "AGENT_LIMIT_EXCEEDED"
    assert [models[name].position for name in models] == [1, 1, 1, 0]


def test_human_resume_keeps_role_budgets_and_evidence_state(control, monkeypatch):
    s = control
    s.settings.knowledge_quality_enabled = True
    router, models = role_models(monkeypatch, s.settings)
    models["answer-model"] = ScriptedModel(responses=[call("delegate_rag", task="NX-1 功率"), final("请补充年份", "clarification")])
    models["grader-model"] = ScriptedModel(responses=[grade("clarify")])
    _, ctx, _, retriever = setup([])
    ctx = replace(ctx, user_id=s.uid, session_id=s.sid)
    claim = s.store.claim_next()
    checkpoint = RunCheckpoint(s.store, claim)
    first = SupervisorWorkflow(s.settings, retriever, router, Web()).run(ctx, [], "", checkpoint=checkpoint)
    assert first.kind == "clarification" and first.run_metadata["model_attempts"] == 4
    paused = s.store.pause(claim, checkpoint.state, first.answer.content)
    s.store.resume(s.uid, s.run_id, 1, paused.resume_key, "2024")
    models.update({"answer-model": ScriptedModel(responses=[call("delegate_rag", task="NX-1 2024 功率"), final("功率100W[2]")]),
                   "fast-model": ScriptedModel(responses=[plan()]),
                   "grader-model": ScriptedModel(responses=[grade(numbers=[2], summary="功率100W[2]")])})
    second = SupervisorWorkflow(s.settings, retriever, router, Web()).run(
        ctx, [], "", checkpoint=RunCheckpoint(s.store, s.store.claim_next()))
    assert second.run_metadata["model_attempts"] == 8
    assert [item["attempt_index"] for item in second.run_metadata["metrics"]["context"]["calls"]] == list(range(1, 9))
    assert [item["number"] for item in second.citations] == [2] and len(retriever.calls) == 2
    assert second.run_metadata["usage_by_model"]["deepseek/answer-model"]["attempts"] == 4


def test_prepared_run_freezes_complete_catalog_before_later_profile_edits(center, monkeypatch):
    from uuid import uuid4
    from app.domain.contracts import AgentChatInput
    s = center
    s.chat.model_center = s.center
    saved = s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(configuration()))
    script = ScriptedModel(responses=[final("冻结模型完成", "smalltalk")])
    seen = []

    def client(implementation, timeout):
        seen.append(implementation.model_name)
        return script

    monkeypatch.setattr(DeepSeekProvider, "agent_client", client)
    s.chat.agent_workflow = SupervisorWorkflow(s.settings, None, None, Web())
    run, _ = s.store.create(s.uid, s.sid, AgentChatInput("fixture", "sql"), "catalog-freeze", uuid4(),
                            queued=True, prepare=s.chat.prepare_run)
    changed = configuration()
    changed["profiles"]["answer-model"]["model_name"] = "changed-after-queue"
    s.center.update_catalog(s.uid, saved["revision"], ModelCatalog.model_validate(changed))
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert message.model_name == "answer-model" and set(seen) == {"answer-model"}
    assert message.run_metadata["model_configuration_revision"] == 2
    assert message.run_metadata["model_roles"]["answer"] == {"profile_id": "answer-model", "profile_version": 1}
    assert s.center.snapshot().catalog.require("answer").version == 2
    assert run.execution_snapshot["model_center"]["catalog"]["profiles"]["answer-model"]["version"] == 1
