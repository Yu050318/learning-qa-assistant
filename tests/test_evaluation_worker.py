import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import func, select

from app.application.chat import ChatService
from app.application.evaluation_worker import EvaluationWorker
from app.application.sessions import SessionService
from app.domain.model_catalog import ModelCatalog
from app.infrastructure.llms.providers import DeepSeekProvider
from app.infrastructure.persistence.models import ChatMessage, ChatRun, EvaluationCaseResult, EvaluationJob, ModelCall
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import ScriptedModel, Web, call, final, report
from test_knowledge_quality import Retriever, grade, plan
from test_model_catalog import configuration
from test_evaluation_jobs import evaluation
from test_model_center import center
from test_chat_runs import system


class JudgeModel(BaseChatModel):
    calls: int = 0
    supported: bool = True

    @property
    def _llm_type(self):
        return "evaluator-fixture"

    def _generate(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1].content)
        claims = []
        if payload["kind"] == "grounded":
            claims = [{"text": payload["answer"], "verdict": "supported" if self.supported else "unsupported",
                "quotes": [{"number": 1, "text": "额定功率100W"}] if self.supported else []}]
        message = AIMessage(content=json.dumps({"id": payload["case"]["id"], "reviewer": "model", "input_sha256": payload["input_sha256"],
            "claims": claims, "fact_coverage": ["covered" if self.supported else "incorrect" for _ in payload["case"]["expected_facts"]]}, ensure_ascii=False))
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def runtime(evaluation, monkeypatch):
    s = evaluation
    s.center.update_catalog(s.uid, 1, ModelCatalog.model_validate(configuration()))
    s.judge = JudgeModel()
    s.models = {"answer-model": ScriptedModel(responses=[call("delegate_rag", task="功率？"), final("功率100W[1]"), final("知识库没有该型号的信息。", kind="insufficient")]),
        "fast-model": ScriptedModel(responses=[plan()]), "grader-model": ScriptedModel(responses=[grade()]), "evaluator-model": s.judge}
    monkeypatch.setattr(DeepSeekProvider, "agent_client", lambda provider, timeout: s.models[provider.model_name])
    s.retriever = Retriever()

    def factory(settings):
        workflow = SupervisorWorkflow(settings, s.retriever, None, Web())
        return SimpleNamespace(settings=settings, agent_workflow=workflow, model_center=s.center,
            chat=ChatService(settings, s.database, SessionService(s.database), workflow), close=lambda: None)

    s.worker = EvaluationWorker(s.settings, s.database, factory)
    return s


def test_worker_executes_frozen_generation_and_independent_evaluator_with_real_ledger(runtime):
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    assert s.worker.run_once()
    finished = s.evaluation.get_job(s.uid, job.id)
    assert finished.state == "succeeded" and finished.completed_cases == 2
    assert finished.report["gate_passed"] and finished.report["summary"]["successful_runs"] == 2
    cases = s.evaluation.cases(s.uid, job.id)
    assert [case.state for case in cases] == ["succeeded", "succeeded"]
    assert cases[0].prediction["evidence"][0]["content"] == "NX-1 额定功率100W"
    assert cases[0].result["metrics"]["faithfulness"] == 1
    assert s.judge.calls == 2
    with s.database.sessions() as session:
        calls = session.scalars(select(ModelCall)).all()
        judges = [row for row in calls if row.operation == "evaluation"]
        assert len(judges) == 2 and all((row.model_role, row.model_profile_id, row.model_profile_version) == ("evaluator", "evaluator-model", 1) for row in judges)
        assert session.scalar(select(func.count()).select_from(ChatRun).where(ChatRun.execution_kind == "evaluation")) == 2
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 4
    assert s.worker.run_once() is False
    assert all(row.id != cases[0].session_id for row in s.sessions.list(s.uid, 0, 20))


def test_recovery_reuses_completed_generation_and_judge_receipt_without_paid_replay(runtime, monkeypatch):
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request.model_copy(update={"split": "dev"}), uuid4())
    original = s.worker._save_case
    monkeypatch.setattr(s.worker, "_save_case", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated process crash")))
    assert s.worker.run_once()
    cases = s.evaluation.cases(s.uid, job.id)
    assert cases[0].state == "running" and cases[0].judge_receipt["state"] == "succeeded"
    assert s.judge.calls == 1
    before = {key: model.position for key, model in s.models.items() if isinstance(model, ScriptedModel)}
    with s.database.sessions() as session:
        session.get(EvaluationJob, job.id).lease_expires_at = s.evaluation.now(session) - timedelta(seconds=1)
        session.commit()
    monkeypatch.setattr(s.worker, "_save_case", original)
    assert s.worker.run_once()
    assert s.evaluation.get_job(s.uid, job.id).state == "succeeded"
    assert s.judge.calls == 1 and before == {key: model.position for key, model in s.models.items() if isinstance(model, ScriptedModel)}
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChatRun)) == 1
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 2


def test_semantically_wrong_answer_fails_quality_gate_even_with_valid_citation_cards(runtime):
    s = runtime
    s.judge.supported = False
    job = s.evaluation.create_job(s.uid, s.request.model_copy(update={"split": "dev"}), uuid4())
    assert s.worker.run_once()
    finished = s.evaluation.get_job(s.uid, job.id)
    assert finished.state == "succeeded" and not finished.report["gate_passed"]
    metrics = finished.report["cases"][0]["metrics"]
    assert metrics["citations_valid"] == 1 and metrics["faithfulness"] == 0 and metrics["fact_coverage"] == 0


def test_cancel_during_generation_stops_delegation_and_settles_original_run(runtime, monkeypatch):
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    original = ScriptedModel._generate

    def cancel_after_first_call(model, messages, *args, **kwargs):
        answer = original(model, messages, *args, **kwargs)
        if model is s.models["answer-model"] and model.position == 1:
            s.evaluation.cancel(s.uid, job.id)
        return answer

    monkeypatch.setattr(ScriptedModel, "_generate", cancel_after_first_call)
    assert s.worker.run_once()
    assert s.evaluation.get_job(s.uid, job.id).state == "cancelled"
    assert s.models["answer-model"].position == 1 and s.models["fast-model"].position == 0 and s.judge.calls == 0
    with s.database.sessions() as session:
        assert session.scalar(select(ChatRun)).state == "cancelled"
        assert session.scalar(select(func.count()).select_from(ChatMessage)) == 2


def test_cancel_before_judge_prevents_the_judge_call(runtime, monkeypatch):
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    original = s.worker._judge

    def cancel(*args):
        s.evaluation.cancel(s.uid, job.id)
        return original(*args)

    monkeypatch.setattr(s.worker, "_judge", cancel)
    assert s.worker.run_once()
    assert s.evaluation.get_job(s.uid, job.id).state == "cancelled" and s.judge.calls == 0
    assert all(case.state == "cancelled" for case in s.evaluation.cases(s.uid, job.id))


def test_model_catalog_edits_after_enqueue_do_not_change_generation_or_evaluator(runtime):
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request.model_copy(update={"split": "dev"}), uuid4())
    data = s.center.catalog_view(s.uid)["catalog"]
    for profile in data["profiles"].values():
        profile["model_name"] = "changed-after-enqueue"
    s.center.update_catalog(s.uid, 2, ModelCatalog.model_validate(data))
    assert s.worker.run_once()
    finished = s.evaluation.get_job(s.uid, job.id)
    assert finished.state == "succeeded" and finished.report["gate_passed"]
    with s.database.sessions() as session:
        assert all(row.model_name != "changed-after-enqueue" and row.configuration_revision == 2 for row in session.scalars(select(ModelCall)))


def test_comparison_requires_same_dataset_scope_and_actual_judge_policy(runtime):
    s = runtime
    request = s.request.model_copy(update={"split": "dev"})
    baseline = s.evaluation.create_job(s.uid, request, uuid4())
    assert s.worker.run_once()
    for model in s.models.values():
        if isinstance(model, ScriptedModel):
            model.position = 0
    candidate = s.evaluation.create_job(s.uid, request.model_copy(update={"baseline_job_id": baseline.id}), uuid4())
    assert s.worker.run_once()
    comparison = s.evaluation.get_job(s.uid, candidate.id).report["comparison"]
    assert not comparison["review_policy_changed"] and comparison["metric_deltas"]["faithfulness"] == 0
    data = s.center.catalog_view(s.uid)["catalog"]
    data["profiles"]["evaluator-model"]["timeout_seconds"] = 20
    s.center.update_catalog(s.uid, 2, ModelCatalog.model_validate(data))
    from app.core.errors import AppError
    with pytest.raises(AppError) as failure:
        s.evaluation.create_job(s.uid, request.model_copy(update={"baseline_job_id": baseline.id}), uuid4())
    assert failure.value.code == "EVALUATION_BASELINE_INCOMPATIBLE"


@pytest.mark.parametrize("change", ["document", "index"])
def test_changed_document_or_index_identity_fails_without_calling_models(runtime, change):
    from app.infrastructure.persistence.models import DocumentRecord
    s = runtime
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    if change == "document":
        with s.database.sessions() as session:
            session.get(DocumentRecord, s.document_id).content_hash = "e" * 64
            session.commit()
    else:
        s.settings.milvus_collection += "_changed"
    assert s.worker.run_once()
    finished = s.evaluation.get_job(s.uid, job.id)
    assert finished.state == "failed" and finished.error_code == "DOCUMENT_SCOPE_CHANGED"
    assert s.models["answer-model"].position == 0 and s.judge.calls == 0
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ChatRun)) == 0


def test_reference_answers_never_enter_generation_prompts_or_run_inputs(runtime):
    from app.application.evaluation_dataset import WorkbenchDataset
    s = runtime
    data = {**s.dataset.content, "name": "label-isolation", "cases": [dict(s.dataset.content["cases"][0])]}
    marker = "EVALUATION_LABEL_SENTINEL_DO_NOT_RETRIEVE"
    data["cases"][0]["reference_answer"] = marker
    dataset = s.evaluation.create_dataset(s.uid, WorkbenchDataset.model_validate(data))
    job = s.evaluation.create_job(s.uid, s.request.model_copy(update={"dataset_id": dataset.id}), uuid4())
    assert s.worker.run_once() and s.evaluation.get_job(s.uid, job.id).state == "succeeded"
    for model in s.models.values():
        if isinstance(model, ScriptedModel):
            assert marker not in str(model.seen_calls)
    with s.database.sessions() as session:
        run = session.scalar(select(ChatRun))
        assert marker not in str(run.execution_snapshot) and run.execution_snapshot["history"] == []


def test_baseline_and_current_execute_their_actual_frozen_retrieval_paths(runtime):
    s = runtime
    s.models["answer-model"].responses = [call("delegate_rag", task="功率？"), call("search_knowledge", query="功率？"), report("功率100W[1]"), final("功率100W[1]")]
    baseline = s.evaluation.create_job(s.uid, s.request.model_copy(update={"split": "dev", "profile": "baseline"}), uuid4())
    assert s.worker.run_once()
    assert s.evaluation.get_job(s.uid, baseline.id).report["gate_passed"]
    assert s.models["fast-model"].position == s.models["grader-model"].position == 0
    s.models["answer-model"].responses = [call("delegate_rag", task="功率？"), final("功率100W[1]")]
    s.models["answer-model"].position = 0
    current = s.evaluation.create_job(s.uid, s.request.model_copy(update={"split": "dev", "baseline_job_id": baseline.id}), uuid4())
    assert s.worker.run_once()
    result = s.evaluation.get_job(s.uid, current.id)
    assert result.report["gate_passed"] and result.report["comparison"]["metric_deltas"]["fact_coverage"] == 0
    assert s.models["fast-model"].position == s.models["grader-model"].position == 1
    assert result.report["usage"]["model_attempts"] == 5 and result.report["usage"]["total_tokens"] is None


def test_confirmed_invalid_evidence_fails_one_case_without_restarting_the_batch(runtime, monkeypatch):
    from dataclasses import replace
    s = runtime
    retrieve = s.retriever.retrieve

    def malformed(*args, **kwargs):
        return [replace(hit, chunk=replace(hit.chunk, chunk_id="invalid-uuid")) for hit in retrieve(*args, **kwargs)]

    monkeypatch.setattr(s.retriever, "retrieve", malformed)
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    assert s.worker.run_once()
    finished = s.evaluation.get_job(s.uid, job.id)
    assert finished.state == "succeeded" and finished.attempts == 1 and not finished.report["gate_passed"]
    cases = s.evaluation.cases(s.uid, job.id)
    assert cases[0].state == "failed" and cases[0].error_code == "EVALUATION_PREDICTION_INVALID"
    assert cases[1].state == "succeeded" and s.judge.calls == 1
