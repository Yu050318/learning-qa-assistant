"""Persistence queries for app/manager/workers.py."""
from sqlalchemy import delete, func, select
from app.entity.do.models import WorkerHeartbeat
from app.dao.postgres.transactions import unwrap


def prune_heartbeats(session, cutoff):
    return unwrap(session).execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.seen_at < cutoff))


def healthy_count(session, kind, configuration_hash, cutoff, worker_id=None):
    query = select(func.count()).select_from(WorkerHeartbeat).where(
        WorkerHeartbeat.kind == kind, WorkerHeartbeat.configuration_hash == configuration_hash,
        WorkerHeartbeat.seen_at >= cutoff)
    if worker_id is not None:
        query = query.where(WorkerHeartbeat.id == worker_id)
    return unwrap(session).scalar(query)


def remove_heartbeat(worker_id, session):
    return unwrap(session).execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.id == worker_id))
