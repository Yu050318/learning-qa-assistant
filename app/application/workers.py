"""Database-clock heartbeats; paths and processing configuration must match the API."""
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.application.ingestion_jobs import aware
from app.infrastructure.persistence.models import WorkerHeartbeat


def ingestion_configuration_hash(settings):
    fields = ("ingestion_mode", "milvus_database", "milvus_collection", "embedding_model", "embedding_dimension",
              "chunk_size", "chunk_overlap", "hierarchy_enabled", "document_versions_enabled", "mineru_enabled", "mineru_model_version")
    payload = {name: getattr(settings, name) for name in fields}
    payload.update(upload_dir=str(settings.upload_dir.resolve()), parsed_dir=str(settings.parsed_dir.resolve()),
                   milvus_uri=settings.milvus_uri)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class WorkerRegistry:
    def __init__(self, settings, database, worker_id=None, *, kind="ingestion"):
        if kind not in {"ingestion", "run", "evaluation"}:
            raise ValueError("Unsupported worker kind")
        self.settings, self.database = settings, database
        self.worker_id = worker_id or uuid4()
        self.kind = kind

    def configuration_hash(self):
        if self.kind == "ingestion":
            return ingestion_configuration_hash(self.settings)
        from app.application.chat import FROZEN_WORKFLOW_SETTINGS
        payload = {name: getattr(self.settings, name) for name in FROZEN_WORKFLOW_SETTINGS}
        payload.update(kind=self.kind, snapshot_version=1, model_center_enabled=self.settings.model_center_enabled,
                       sharing_enabled=self.settings.sharing_enabled, sql_allowed_tables=self.settings.sql_allowed_tables,
                       milvus_uri=self.settings.milvus_uri, milvus_database=self.settings.milvus_database,
                       milvus_collection=self.settings.milvus_collection)
        if self.kind == "evaluation":
            payload.update(evaluation_protocol=1, evaluation_enabled=self.settings.evaluation_enabled)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def now(session):
        clock = func.clock_timestamp() if session.get_bind().dialect.name == "postgresql" else func.current_timestamp()
        return aware(session.scalar(select(clock)))

    def beat(self):
        with self.database.sessions() as session:
            row = session.get(WorkerHeartbeat, self.worker_id)
            if row is None:
                row = WorkerHeartbeat(id=self.worker_id, kind=self.kind)
                session.add(row)
            if row.kind != self.kind:
                raise ValueError("Worker identity cannot change kind")
            row.configuration_hash = self.configuration_hash()
            row.seen_at = self.now(session)
            # Expired rows are diagnostic-free liveness records, not job history.
            session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.seen_at < self.now(session) - timedelta(days=1)))
            session.commit()

    def healthy_count(self, worker_id=None):
        with self.database.sessions() as session:
            query = select(func.count()).select_from(WorkerHeartbeat).where(
                WorkerHeartbeat.kind == self.kind,
                WorkerHeartbeat.configuration_hash == self.configuration_hash(),
                WorkerHeartbeat.seen_at >= self.now(session) - timedelta(seconds=45))
            if worker_id is not None:
                query = query.where(WorkerHeartbeat.id == worker_id)
            return session.scalar(query)

    def stop(self):
        with self.database.sessions() as session:
            session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.id == self.worker_id))
            session.commit()
