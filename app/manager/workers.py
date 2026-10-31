from app.dao.postgres import transactions
"""Database-clock heartbeats; paths and processing configuration must match the API."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import workers as queries
import hashlib
import json
from datetime import timedelta
from uuid import uuid4


from app.entity.time import aware
from app.entity.do.models import WorkerHeartbeat


def ingestion_configuration_hash(settings):
    fields = ("ingestion_mode", "milvus_database", "milvus_collection", "embedding_model", "embedding_dimension",
              "chunk_size", "chunk_overlap", "hierarchy_enabled", "document_versions_enabled", "document_publication_enabled", "workspaces_enabled", "mineru_enabled", "mineru_model_version")
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
        from app.entity.execution import FROZEN_WORKFLOW_SETTINGS
        payload = {name: getattr(self.settings, name) for name in FROZEN_WORKFLOW_SETTINGS}
        payload.update(kind=self.kind, snapshot_version=1, model_center_enabled=self.settings.model_center_enabled,
                       document_publication_enabled=self.settings.document_publication_enabled,
                       workspaces_enabled=self.settings.workspaces_enabled,
                       capability_center_enabled=self.settings.capability_center_enabled,
                       capability_secret_names=sorted(self.settings.capability_secret_names),
                       sharing_enabled=self.settings.sharing_enabled, sql_allowed_tables=self.settings.sql_allowed_tables,
                       milvus_uri=self.settings.milvus_uri, milvus_database=self.settings.milvus_database,
                       milvus_collection=self.settings.milvus_collection)
        if self.kind == "evaluation":
            payload.update(evaluation_protocol=1, evaluation_enabled=self.settings.evaluation_enabled)
        if self.settings.tool_approvals_enabled:
            payload.update(tool_approvals_enabled=True, approval_protocol=1)
        if self.settings.sandbox_enabled:
            payload.update(sandbox_enabled=True, sandbox_protocol=2,
                sandbox_require_rootless=self.settings.sandbox_require_rootless)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def now(session):
        return transactions.database_now(session)

    def beat(self):
        with transaction_scope(self.database) as session:
            row = as_transaction(session).load(WorkerHeartbeat, self.worker_id)
            if row is None:
                row = WorkerHeartbeat(id=self.worker_id, kind=self.kind)
                as_transaction(session).save(row)
            if row.kind != self.kind:
                raise ValueError("Worker identity cannot change kind")
            row.configuration_hash = self.configuration_hash()
            row.seen_at = self.now(session)
            # Expired rows are diagnostic-free liveness records, not job history.
            queries.prune_heartbeats(session, self.now(session) - timedelta(days=1))
            session.commit()

    def healthy_count(self, worker_id=None):
        with transaction_scope(self.database) as session:
            return queries.healthy_count(session, self.kind, self.configuration_hash(),
                self.now(session) - timedelta(seconds=45), worker_id)

    def stop(self):
        with transaction_scope(self.database) as session:
            queries.remove_heartbeat(self.worker_id, session)
            session.commit()
