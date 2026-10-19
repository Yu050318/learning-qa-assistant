"""Owner-scoped, durable evaluation control plane. No inference in API methods."""
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import and_, func, or_, select

from app.application.chat import workflow_settings_snapshot
from app.application.evaluation import StrictModel, digest
from app.application.evaluation_judge import judge_prompt
from app.application.evaluation_gates import EvaluationGatePolicy
from app.application.evaluation_dataset import WorkbenchDataset
from app.application.ingestion_jobs import aware
from app.application.model_center import ModelCenter, catalog_from_legacy
from app.application.runs import RunStore, bounded_json
from app.application.sharing import SharingService
from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import ChatRun, DocumentRecord, EvaluationCaseResult, EvaluationDataset, EvaluationJob, KnowledgeDocument, User
from app.infrastructure.persistence.repository import Repository

ACTIVE = ("queued", "running", "cancelling")
TERMINAL = ("succeeded", "failed", "cancelled")


def index_identity(settings):
    return digest({name: getattr(settings, name) for name in (
        "milvus_uri", "milvus_database", "milvus_collection", "embedding_model", "embedding_dimension")})


class EvaluationJobCreate(StrictModel):
    dataset_id: UUID
    baseline_job_id: UUID | None = None
    split: Literal["all", "dev", "frozen", "custom"] = "all"
    profile: Literal["baseline", "current"] = "current"
    gate_policy: EvaluationGatePolicy = Field(default_factory=EvaluationGatePolicy)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def explicit_scope(self):
        if not self.document_ids and not self.knowledge_base_ids:
            raise ValueError("Evaluation requires an explicit authorized document/knowledge-base scope")
        if len(set(self.document_ids)) != len(self.document_ids) or len(set(self.knowledge_base_ids)) != len(self.knowledge_base_ids):
            raise ValueError("Evaluation scope must not contain duplicate IDs")
        if self.baseline_job_id is None and (self.gate_policy.require_comparison or any(gate.minimum is None and gate.maximum is None for gate in self.gate_policy.metric_gates)):
            raise ValueError("Comparison-only gates require a baseline job")
        return self


@dataclass(frozen=True)
class EvaluationClaim:
    job_id: UUID
    user_id: UUID
    token: UUID
    generation: int


class EvaluationService:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database
        self.runs = RunStore(settings, database, kind="evaluation")

    def require(self):
        if not self.settings.evaluation_enabled:
            raise AppError("EVALUATION_DISABLED", "评测工作台尚未启用", 503)
        if not self.settings.chat_runs_enabled or not self.settings.model_center_enabled:
            raise AppError("EVALUATION_CONFIGURATION_REQUIRED", "评测需要模型中心及持久 Run", 503)

    def admin(self, session, uid, *, lock=False):
        self.require()
        query = select(User).where(User.id == uid)
        user = session.scalar(query.with_for_update() if lock else query)
        if user is None or not user.is_active or user.role != "admin":
            raise AppError("FORBIDDEN", "需要平台管理员权限", 403)

    def now(self, session):
        return self.runs.now(session)

    def create_dataset(self, uid, dataset):
        dataset = WorkbenchDataset.model_validate(dataset.model_dump())
        content = bounded_json(dataset.model_dump(mode="json"), 8_000_000)
        with self.database.sessions() as session:
            self.admin(session, uid, lock=True)
            existing = session.scalar(select(EvaluationDataset).where(
                EvaluationDataset.created_by == uid, EvaluationDataset.fingerprint == dataset.fingerprint))
            if existing is not None:
                return existing
            row = EvaluationDataset(created_by=uid, name=dataset.name, fingerprint=dataset.fingerprint,
                                    case_count=len(dataset.cases), content=content)
            session.add(row)
            session.commit()
            return row

    def dataset(self, session, uid, dataset_id):
        row = session.scalar(select(EvaluationDataset).where(EvaluationDataset.id == dataset_id, EvaluationDataset.created_by == uid))
        if row is None:
            raise not_found()
        return row

    def datasets(self, uid, offset=0, limit=20):
        with self.database.sessions() as session:
            self.admin(session, uid)
            return session.scalars(select(EvaluationDataset).where(EvaluationDataset.created_by == uid)
                .order_by(EvaluationDataset.created_at.desc(), EvaluationDataset.id).offset(offset).limit(limit)).all()

    def get_dataset(self, uid, dataset_id):
        with self.database.sessions() as session:
            self.admin(session, uid)
            return self.dataset(session, uid, dataset_id)

    def job(self, session, uid, job_id, *, lock=False):
        query = select(EvaluationJob).where(EvaluationJob.id == job_id, EvaluationJob.created_by == uid)
        row = session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise not_found()
        return row

    def get_job(self, uid, job_id):
        with self.database.sessions() as session:
            self.admin(session, uid)
            return self.job(session, uid, job_id)

    def jobs(self, uid, offset=0, limit=20, state=None):
        with self.database.sessions() as session:
            self.admin(session, uid)
            query = select(EvaluationJob).where(EvaluationJob.created_by == uid)
            if state is not None:
                query = query.where(EvaluationJob.state == state)
            return session.scalars(query.order_by(EvaluationJob.created_at.desc(), EvaluationJob.id).offset(offset).limit(limit)).all()

    def cases(self, uid, job_id, offset=0, limit=50):
        with self.database.sessions() as session:
            self.admin(session, uid)
            self.job(session, uid, job_id)
            return session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.job_id == job_id)
                .order_by(EvaluationCaseResult.position).offset(offset).limit(limit)).all()

    def create_job(self, uid, payload, client_key):
        payload = EvaluationJobCreate.model_validate(payload.model_dump())
        fingerprint = digest(payload.model_dump(mode="json"))
        with self.database.sessions() as session:
            self.admin(session, uid, lock=True)
            previous = session.scalar(select(EvaluationJob).where(EvaluationJob.created_by == uid, EvaluationJob.client_key == client_key))
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise AppError("IDEMPOTENCY_CONFLICT", "评测请求标识已用于其他配置", 409)
                return previous
            count = session.scalar(select(func.count()).select_from(EvaluationJob).where(EvaluationJob.created_by == uid, EvaluationJob.state.in_(ACTIVE)))
            if count >= self.settings.evaluation_queue_limit:
                raise AppError("EVALUATION_CAPACITY_EXCEEDED", "待处理评测任务已达上限", 429)
            row = self.dataset(session, uid, payload.dataset_id)
            dataset = WorkbenchDataset.model_validate(row.content)
            labels = [case for case in dataset.cases if payload.split == "all" or case.split == payload.split]
            if not labels:
                raise AppError("EVALUATION_EMPTY_SPLIT", "所选分组没有题目", 422)
            center = ModelCenter(self.settings, self.database)
            snapshot = center.snapshot(session=session)
            catalog = snapshot.catalog or catalog_from_legacy(snapshot.configuration.model_dump(mode="json"))
            settings = workflow_settings_snapshot(snapshot.settings)
            if payload.profile == "baseline":
                settings.update(retrieval_parent_context=False, hierarchy_enabled=False, knowledge_quality_enabled=False, rerank_enabled=False)
            settings["web_search_enabled"] = False
            if payload.knowledge_base_ids:
                owners = SharingService(self.settings, self.database).scope(uid, payload.knowledge_base_ids, payload.document_ids or None, session=session, lock=True)
                if not payload.document_ids:
                    shared = set(session.scalars(select(KnowledgeDocument.document_id).where(KnowledgeDocument.knowledge_base_id.in_(payload.knowledge_base_ids))))
                    owners = {did: owner for did, owner in owners.items() if did in shared}
            else:
                ids = Repository(session).ready_document_ids(uid, payload.document_ids, self.settings.embedding_model,
                    **({"current_only": True} if self.settings.document_versions_enabled else {}))
                if set(ids) != set(payload.document_ids):
                    raise AppError("DOCUMENT_SCOPE_CHANGED", "评测资料未就绪或无权访问", 409)
                owners = {did: uid for did in ids}
            if not owners:
                raise AppError("EVALUATION_EMPTY_SCOPE", "评测知识库没有可检索资料", 422)
            documents = session.scalars(select(DocumentRecord).where(DocumentRecord.id.in_(owners)).order_by(DocumentRecord.id)).all()
            names = {}
            for document in documents:
                names.setdefault(document.original_name, []).append(document)
            for label in labels:
                for anchor in label.gold_anchors:
                    if len(names.get(anchor.canonical_name, [])) != 1:
                        raise AppError("EVALUATION_GOLD_SCOPE_INVALID", "标准来源缺失或文件名不唯一，请明确资料范围", 422)
            scope = bounded_json({"document_ids": sorted(map(str, owners)), "document_owners": {str(key): str(value) for key, value in owners.items()},
                "knowledge_base_ids": list(map(str, payload.knowledge_base_ids)), "documents": [{"id": str(doc.id), "owner_id": str(doc.user_id),
                "canonical_name": doc.original_name, "content_hash": doc.content_hash, "embedding_model": doc.embedding_model} for doc in documents]}, 200_000)
            frozen = bounded_json({"version": 1, "revision": snapshot.revision, "catalog": catalog.model_dump(mode="json"),
                "answer_profile_id": snapshot.answer_profile_id or catalog.assignments["answer"], "settings": settings,
                "judge_prompt_sha256": digest(judge_prompt()), "index_sha256": index_identity(self.settings)}, 200_000)
            evaluator = catalog.require("evaluator")
            frozen["judge_policy_sha256"] = digest({"profile": evaluator.model_dump(mode="json"),
                "fallback": catalog.profiles[evaluator.fallback_profile_id].model_dump(mode="json") if evaluator.fallback_profile_id else None,
                "prompt": frozen["judge_prompt_sha256"], "input_budget": settings["agent_context_token_budget"]})
            if payload.baseline_job_id is not None:
                baseline = self.job(session, uid, payload.baseline_job_id)
                if baseline.state != "succeeded" or baseline.report is None or baseline.dataset_fingerprint != dataset.fingerprint or baseline.split != payload.split or baseline.scope_snapshot != scope:
                    raise AppError("EVALUATION_BASELINE_INCOMPATIBLE", "基线必须成功且使用同一标注集、分组和资料身份", 409)
                if baseline.model_snapshot["judge_policy_sha256"] != frozen["judge_policy_sha256"] or baseline.model_snapshot["index_sha256"] != frozen["index_sha256"]:
                    raise AppError("EVALUATION_BASELINE_INCOMPATIBLE", "对比任务必须使用同一评测模型、审阅策略及索引身份", 409)
            job = EvaluationJob(id=uuid4(), created_by=uid, dataset_id=row.id, client_key=client_key,
                fingerprint=fingerprint, dataset_fingerprint=dataset.fingerprint, baseline_job_id=payload.baseline_job_id,
                split=payload.split, profile=payload.profile, total_cases=len(labels), model_snapshot=frozen, scope_snapshot=scope,
                gate_policy=payload.gate_policy.model_dump(mode="json"), available_at=self.now(session))
            session.add(job)
            session.flush()
            session.add_all([EvaluationCaseResult(job_id=job.id, case_id=label.id, position=index, label=label.model_dump(mode="json")) for index, label in enumerate(labels)])
            session.commit()
            return job

    def owned(self, session, claim, *, allow_cancelling=False, authorize=True):
        row = self.job(session, claim.user_id, claim.job_id, lock=True)
        if (row.owner_token != claim.token or row.generation != claim.generation or row.lease_expires_at is None
                or aware(row.lease_expires_at) <= self.now(session) or row.state not in {"running", "cancelling"}):
            raise AppError("EVALUATION_LEASE_LOST", "评测任务租约已失效", 409)
        if row.state == "cancelling" and not allow_cancelling:
            raise AppError("EVALUATION_CANCEL_REQUESTED", "评测已请求取消", 409)
        if authorize:
            self.admin(session, claim.user_id)
        return row

    def check_owned(self, claim):
        with self.database.sessions() as session:
            row = self.owned(session, claim)
            self.validate_scope(session, row)

    def validate_scope(self, session, job):
        if job.model_snapshot["index_sha256"] != index_identity(self.settings):
            raise AppError("DOCUMENT_SCOPE_CHANGED", "评测索引连接、集合或向量模型已变化", 409)
        expected = job.scope_snapshot
        ids = [UUID(value) for value in expected["document_ids"]]
        documents = session.scalars(select(DocumentRecord).where(DocumentRecord.id.in_(ids))).all()
        observed = {str(doc.id): {"id": str(doc.id), "owner_id": str(doc.user_id), "canonical_name": doc.original_name,
            "content_hash": doc.content_hash, "embedding_model": doc.embedding_model} for doc in documents if doc.status == "ready"}
        if observed != {item["id"]: item for item in expected["documents"]}:
            raise AppError("DOCUMENT_SCOPE_CHANGED", "评测冻结的资料身份或可检索状态已变化", 409)
        if expected["knowledge_base_ids"]:
            try:
                owners = SharingService(self.settings, self.database).scope(job.created_by,
                    [UUID(value) for value in expected["knowledge_base_ids"]], ids, session=session)
            except AppError as error:
                if error.status_code in {403, 404}:
                    raise AppError("DOCUMENT_SCOPE_CHANGED", "评测共享授权已变化", 409) from None
                raise
            if {str(key): str(value) for key, value in owners.items()} != expected["document_owners"]:
                raise AppError("DOCUMENT_SCOPE_CHANGED", "评测共享授权已变化", 409)
        elif any(doc.user_id != job.created_by for doc in documents):
            raise AppError("DOCUMENT_SCOPE_CHANGED", "评测资料所有权已变化", 409)

    def heartbeat(self, claim):
        with self.database.sessions() as session:
            row = self.owned(session, claim, allow_cancelling=True)
            row.lease_expires_at = self.now(session) + timedelta(seconds=self.settings.evaluation_lease_seconds)
            session.commit()

    def _finish(self, session, row, state, code=None):
        row.state, row.error_code, row.finished_at = state, code, self.now(session)
        row.owner_token = row.lease_expires_at = None
        unfinished = session.scalars(select(EvaluationCaseResult).where(EvaluationCaseResult.job_id == row.id,
            EvaluationCaseResult.state.in_(("queued", "running")))).all()
        for case in unfinished:
            case.state, case.error_code, case.finished_at = "cancelled" if state == "cancelled" else "failed", code, row.finished_at
        row.completed_cases = row.total_cases

    def claim_next(self):
        self.require()
        # Reconcile a crash between finishing a job and cancelling its child
        # Runs. No job lock is held while acquiring Run locks.
        with self.database.sessions() as session:
            abandoned = session.execute(select(EvaluationJob.created_by, ChatRun.id).join(EvaluationCaseResult,
                EvaluationCaseResult.job_id == EvaluationJob.id).join(ChatRun, ChatRun.id == EvaluationCaseResult.run_id)
                .where(EvaluationJob.state.in_(TERMINAL), ChatRun.execution_kind == "evaluation", ChatRun.state.in_(("queued", "running", "waiting", "cancelling"))).limit(10)).all()
        for uid, run_id in abandoned:
            self.runs.request_cancel(uid, run_id)
        with self.database.sessions() as session:
            self.runs.settle_cancellations(session)
            now = self.now(session)
            query = select(EvaluationJob).where(or_(
                and_(EvaluationJob.state == "queued", EvaluationJob.available_at <= now),
                and_(EvaluationJob.state.in_(("running", "cancelling")), EvaluationJob.lease_expires_at <= now)))
            for row in session.scalars(query.order_by(EvaluationJob.created_at, EvaluationJob.id).limit(10).with_for_update(skip_locked=True)):
                if row.state == "cancelling":
                    self._finish(session, row, "cancelled", "EVALUATION_CANCEL_REQUESTED")
                    continue
                if row.attempts >= row.max_attempts:
                    self._finish(session, row, "failed", "EVALUATION_RECOVERY_LIMIT")
                    continue
                row.state, row.owner_token = "running", uuid4()
                row.generation += 1
                row.attempts += 1
                row.started_at = row.started_at or now
                row.lease_expires_at = now + timedelta(seconds=self.settings.evaluation_lease_seconds)
                claim = EvaluationClaim(row.id, row.created_by, row.owner_token, row.generation)
                session.commit()
                return claim
            session.commit()
            return None

    def cancel(self, uid, job_id):
        with self.database.sessions() as session:
            self.admin(session, uid)
            row = self.job(session, uid, job_id, lock=True)
            if row.state == "queued":
                self._finish(session, row, "cancelled", "EVALUATION_CANCEL_REQUESTED")
            elif row.state == "running":
                row.state = "cancelling"
            run_ids = list(session.scalars(select(EvaluationCaseResult.run_id).where(EvaluationCaseResult.job_id == row.id, EvaluationCaseResult.run_id.is_not(None))))
            session.commit()
        for run_id in run_ids:
            self.runs.request_cancel(uid, run_id)
        return self.get_job(uid, job_id)
