"""One independent evaluation slot, reusing durable generation receipts."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import evaluation_worker as queries
import logging
from datetime import timedelta
from threading import Event, Thread
from time import monotonic
from uuid import UUID, uuid5
from statistics import median
from math import ceil


from pydantic import ValidationError

from app.entity.evaluation.evaluation import Prediction, SemanticReview, compare_reports, digest, evaluate, review_digest, score_case
from app.entity.evaluation.evaluation_dataset import WorkbenchCase
from app.service.evaluation_jobs import EvaluationService
from app.entity.evaluation.evaluation_gates import evaluate_gates
from app.entity.time import aware
from app.service.evaluation_judge import judge, judge_prompt
from app.service.model_center import ModelSnapshot, legacy_projection
from app.core.serialization import bounded_json
from app.core.errors import AppError
from app.entity.bo.contracts import AgentChatInput
from app.entity.bo.model_catalog import ModelCatalog
from app.entity.do.models import ChatMessage, ChatRunEvent, ChatSession, EvaluationCaseResult, ModelCall

logger = logging.getLogger("rag")


class EvaluationWorker:
    def __init__(self, settings, database, services_factory=None):
        if services_factory is None:
            raise ValueError("EvaluationWorker requires an injected services_factory")
        self.settings, self.database, self.services_factory = settings, database, services_factory
        self.store = EvaluationService(settings, database)
        self.stop = Event()

    def _defer(self, claim):
        with transaction_scope(self.database) as session:
            job = self.store.owned(session, claim)
            job.state, job.owner_token, job.lease_expires_at = "queued", None, None
            job.available_at = self.store.now(session) + timedelta(seconds=1)
            job.attempts = max(0, job.attempts - 1)  # Waiting for a surviving Run lease consumes no recovery attempt.
            session.commit()

    def _case_run(self, claim, case_id, services, snapshot):
        with transaction_scope(self.database) as session:
            job = self.store.owned(session, claim)
            self.store.validate_scope(session, job)
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            if case.job_id != job.id:
                raise AppError("EVALUATION_CASE_INVALID", "评测题目与任务不一致", 409)
            if case.run_id is not None:
                return case.run_id
            if case.session_id is None:
                conversation = ChatSession(user_id=claim.user_id, title=f"评测 · {case.case_id}"[:200], kind="evaluation")
                as_transaction(session).save(conversation)
                session.flush()
                case.session_id = conversation.id
            case.state, case.started_at = "running", case.started_at or self.store.now(session)
            sid, label, scope = case.session_id, WorkbenchCase.model_validate(case.label), job.scope_snapshot
            session.commit()
        payload = AgentChatInput(question=label.question, search_mode="knowledge", model_profile_id=snapshot.answer_profile_id,
            document_ids=tuple(UUID(value) for value in scope["document_ids"]),
            knowledge_base_ids=tuple(UUID(value) for value in scope["knowledge_base_ids"]))

        def prepare(session, run, payload):
            job = self.store.owned(session, claim)
            self.store.validate_scope(session, job)
            options = {"frozen_snapshot": snapshot}
            if job.model_snapshot.get("capabilities") is not None:
                options["frozen_capabilities"] = job.model_snapshot["capabilities"]
            services.chat.prepare_run(session, run, payload, **options)
            if set(run.execution_snapshot["context"]["allowed_document_ids"]) != set(job.scope_snapshot["document_ids"]):
                raise AppError("DOCUMENT_SCOPE_CHANGED", "评测检索范围与冻结范围不一致", 409)
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            if case.run_id is not None and case.run_id != run.id:
                raise AppError("EVALUATION_CASE_INVALID", "评测题目已有不同运行", 409)
            case.run_id = run.id

        run, _ = self.store.runs.create(claim.user_id, sid, payload, f"evaluation-{case_id}", uuid5(claim.job_id, str(case_id)), queued=True, prepare=prepare)
        return run.id

    def _generate(self, claim, case_id, services, snapshot):
        run_id = self._case_run(claim, case_id, services, snapshot)
        run, _ = self.store.runs.read(claim.user_id, run_id)
        if run.state in {"queued", "running"}:
            run_claim = self.store.runs.claim_next(run_id)
            if run_claim is None:
                return None
            stop = Event()

            def heartbeat():
                while not stop.wait(self.settings.chat_run_lease_seconds / 3):
                    try:
                        self.store.check_owned(claim)
                        self.store.runs.heartbeat(run_claim)
                    except Exception:
                        return

            thread = Thread(target=heartbeat, name="evaluation-run-lease", daemon=True)
            thread.start()
            try:
                services.chat.execute_prepared(self.store.runs, run_claim, validate_external=lambda: self.store.check_owned(claim))
            except AppError as error:
                if error.code == "EVALUATION_CANCEL_REQUESTED":
                    self.store.runs.request_cancel(claim.user_id, run_id)
                    self.store.runs.finish_failure(run_claim, error)
                    raise
                if error.code in {"EVALUATION_LEASE_LOST", "EVALUATION_CANCEL_REQUESTED", "RUN_LEASE_LOST"}:
                    raise
                if error.code == "RESOURCE_BUSY":
                    self.store.runs.release_busy(run_claim)
                    return None
                self.store.runs.finish_failure(run_claim, error)
            except Exception as error:
                self.store.runs.finish_failure(run_claim, error)
            finally:
                stop.set()
                thread.join(timeout=self.settings.database_timeout + 1)
            run, _ = self.store.runs.read(claim.user_id, run_id)
        self.store.check_owned(claim)
        with transaction_scope(self.database) as session:
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            message = as_transaction(session).load(ChatMessage, run.message_id) if run.message_id else None
            if run.state not in {"succeeded", "waiting"} or message is None:
                event = queries.latest_run_error(run, session)
                return Prediction(id=case.case_id, error_code=event.payload.get("code") if event else f"GENERATION_{run.state.upper()}")
            try:
                prediction = Prediction(id=case.case_id, answer=message.content, kind=run.result_snapshot["kind"],
                    citations=message.citations, evidence=run.result_snapshot.get("evidence", []), run_metadata=message.run_metadata,
                    provider=message.model_provider, model=message.model_name)
            except (ValidationError, KeyError, TypeError):
                # The generation has a confirmed terminal result. A malformed
                # result is a case failure, not an unknown call to retry.
                return Prediction(id=case.case_id, error_code="EVALUATION_PREDICTION_INVALID")
        if run.state == "waiting":
            # Dataset cases are independent trials. A clarification is scored
            # as such, without silently fabricating a human answer.
            self.store.runs.request_cancel(claim.user_id, run.id)
        return prediction

    def _judge(self, claim, case_id, prediction, services, snapshot):
        with transaction_scope(self.database) as session:
            self.store.owned(session, claim)
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            label = WorkbenchCase.model_validate(case.label)
            identity = digest({"input": review_digest(label, prediction), "prompt": digest(judge_prompt()),
                "catalog": snapshot.catalog.model_dump(mode="json")})
            receipt = case.judge_receipt or {}
            if receipt and receipt["input_hash"] != identity:
                raise AppError("EVALUATION_REVIEW_DIVERGED", "审阅输入与已保存凭据不一致", 409)
            if receipt.get("state") == "succeeded":
                return SemanticReview.model_validate(receipt["review"])
            attempts = receipt.get("attempts", 0) + 1
            if attempts > 3:
                raise AppError("EVALUATION_REVIEW_RECOVERY_LIMIT", "评测模型恢复次数已耗尽", 409)
            if receipt.get("state") == "started" and receipt.get("generation") == claim.generation:
                raise AppError("EVALUATION_REVIEW_BUSY", "本实例已有未确认的审阅调用", 409)
            case.prediction = prediction.model_dump(mode="json")
            case.judge_receipt = {"state": "started", "input_hash": identity, "attempts": attempts, "generation": claim.generation}
            sid = case.session_id
            session.commit()
        router = snapshot.router().for_role("evaluator")
        observer = services.model_center.observer(claim.user_id, sid, f"eval-judge-{case_id}-{attempts}", snapshot.revision, "evaluation")
        review = judge(label, prediction, snapshot.settings, router, observer, lambda: self.store.check_owned(claim))
        with transaction_scope(self.database) as session:
            self.store.owned(session, claim)
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            case.judge_receipt = {**case.judge_receipt, "state": "succeeded", "review": review.model_dump(mode="json")}
            session.commit()
        return review

    def _save_case(self, claim, case_id, prediction, review, elapsed, error=None):
        with transaction_scope(self.database) as session:
            job = self.store.owned(session, claim)
            self.store.validate_scope(session, job)
            case = as_transaction(session).load(EvaluationCaseResult, case_id)
            label = WorkbenchCase.model_validate(case.label)
            case.prediction = bounded_json(prediction.model_dump(mode="json"), 4_000_000)
            case.review = review.model_dump(mode="json") if review else None
            case.result = score_case(label, prediction, review)
            case.state = "failed" if prediction.error_code or error else "succeeded"
            if error and case.judge_receipt and case.judge_receipt.get("state") == "started":
                case.judge_receipt = {**case.judge_receipt, "state": "failed", "error_code": error}
            case.error_code, case.finished_at = error or prediction.error_code, self.store.now(session)
            case.duration_ms = max(0, round((case.finished_at - aware(case.started_at)).total_seconds() * 1000)) if case.started_at else elapsed
            session.flush()
            job.completed_cases = queries.finished_case_count(job, session)
            session.commit()

    def _finish(self, claim):
        with transaction_scope(self.database) as session:
            job = self.store.owned(session, claim)
            self.store.validate_scope(session, job)
            cases = queries.ordered_cases(job, session)
            labels = [WorkbenchCase.model_validate(case.label) for case in cases]
            predictions = [Prediction.model_validate(case.prediction) for case in cases if case.prediction]
            report = evaluate(labels, predictions,
                [SemanticReview.model_validate(case.review) for case in cases if case.review])
            for row in report["cases"]:
                if row.get("judge_policy"):
                    row["judge_policy"]["configuration_sha256"] = job.model_snapshot["judge_policy_sha256"]
            report["scope_sha256"] = digest(job.scope_snapshot)
            report["model_snapshot_sha256"] = digest(job.model_snapshot)
            report["execution_failed_cases"] = sum(case.state != "succeeded" for case in cases)
            durations = sorted(case.duration_ms for case in cases if case.duration_ms is not None)
            report["timing"] = {"definition": "case wall time, including lease recovery waits; excludes job queue wait",
                "median_ms": median(durations) if durations else None,
                "p95_ms": durations[ceil(len(durations) * .95) - 1] if durations else None}
            calls = queries.case_model_calls(cases, session)
            reported = [row for row in calls if row.total_tokens is not None]
            report["usage"] = {"model_attempts": len(calls), "reported_calls": len(reported),
                "total_tokens": sum(row.total_tokens for row in reported) if len(reported) == len(calls) and calls else None,
                "reported_total_tokens": sum(row.total_tokens for row in reported) if reported else None}
            if job.baseline_job_id:
                baseline = self.store.job(session, claim.user_id, job.baseline_job_id)
                report["comparison"] = compare_reports(report, baseline.report)
            report["gate_policy"] = job.gate_policy
            report["gate_checks"] = evaluate_gates(job.gate_policy, report, labels, predictions)
            report["gate_passed"] = all(check["passed"] for check in report["gate_checks"])
            job.report = bounded_json(report, 8_000_000)
            job.state, job.finished_at = "succeeded", self.store.now(session)
            job.owner_token = job.lease_expires_at = None
            session.commit()

    def run_once(self):
        claim = self.store.claim_next()
        if claim is None:
            return False
        stop = Event()

        def heartbeat():
            while not stop.wait(self.settings.evaluation_lease_seconds / 3):
                try:
                    self.store.heartbeat(claim)
                except Exception:
                    return

        thread = Thread(target=heartbeat, name="evaluation-job-lease", daemon=True)
        thread.start()
        services = None
        try:
            with transaction_scope(self.database) as session:
                job = self.store.owned(session, claim)
                frozen = job.model_snapshot
                if frozen["judge_prompt_sha256"] != digest(judge_prompt()):
                    raise AppError("EVALUATION_POLICY_CHANGED", "评测审阅策略已变化，请创建新任务", 409)
                cases = queries.unfinished_case_ids(job, session)
            settings = self.settings.model_copy(update={**frozen["settings"], "evaluation_enabled": False})
            catalog = ModelCatalog.model_validate(frozen["catalog"])
            snapshot = ModelSnapshot(settings, frozen["revision"], legacy_projection(catalog, settings), {}, catalog, frozen["answer_profile_id"])
            services = self.services_factory(settings)
            services.agent_workflow.memories = None
            for case_id in cases:
                if self.stop.is_set():
                    self._defer(claim)
                    return True
                started = monotonic()
                prediction = self._generate(claim, case_id, services, snapshot)
                if prediction is None:
                    self._defer(claim)
                    return True
                review, error = None, None
                if not prediction.error_code:
                    try:
                        review = self._judge(claim, case_id, prediction, services, snapshot)
                    except AppError as failure:
                        if failure.code in {"EVALUATION_LEASE_LOST", "EVALUATION_CANCEL_REQUESTED", "DOCUMENT_SCOPE_CHANGED", "FORBIDDEN"}:
                            raise
                        error = failure.code
                    except ValueError:
                        error = "EVALUATION_REVIEW_INVALID"
                self._save_case(claim, case_id, prediction, review, round((monotonic() - started) * 1000), error)
            self._finish(claim)
        except AppError as error:
            if error.code == "RUN_LEASE_LOST":
                self._defer(claim)
            elif error.code != "EVALUATION_LEASE_LOST":
                with transaction_scope(self.database) as session:
                    job = self.store.owned(session, claim, allow_cancelling=True, authorize=False)
                    self.store._finish(session, job, "cancelled" if job.state == "cancelling" else "failed", error.code)
                    session.commit()
        except Exception as error:
            # Leave uncertain calls attached to the existing leases/receipts;
            # a bounded takeover can recover without creating new Runs.
            logger.warning("evaluation_attempt_not_settled", extra={"resource_id": str(claim.job_id), "error_type": type(error).__name__})
        finally:
            stop.set()
            thread.join(timeout=self.settings.database_timeout + 1)
            if services:
                services.close()
        return True
