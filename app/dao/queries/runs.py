"""Persistence queries for app/service/runs.py."""
from sqlalchemy import and_, func, or_, select
import json
from app.entity.do.models import ChatEventOutbox, ChatMessage, ChatRun, ChatRunCheckpoint, ChatRunEvent, ChatRunOperation, User
from app.entity.do.models import RunArtifact
from app.dao.postgres.transactions import unwrap


def get_run(kind, lock, run_id, session, user_id):
    session = unwrap(session)
    query = select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id, ChatRun.execution_kind == kind)
    return session.scalar(query.with_for_update() if lock else query)


def lock_user(session, user_id):
    session = unwrap(session)
    return (session.scalar(select(User).where(User.id == user_id).with_for_update()))


def find_idempotent_run(client_key, session, user_id):
    session = unwrap(session)
    return (session.scalar(select(ChatRun).where(ChatRun.user_id == user_id, ChatRun.client_key == client_key).with_for_update()))


def active_session_run(ACTIVE_STATES, session, session_id):
    session = unwrap(session)
    return (session.scalar(select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.state.in_(ACTIVE_STATES)).with_for_update()))


def count_active_runs(ACTIVE_STATES, kind, session, user_id):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(ChatRun).where(
                        ChatRun.user_id == user_id, ChatRun.execution_kind == kind, ChatRun.state.in_(ACTIVE_STATES))))


def list_events_after(after, run_id, session):
    session = unwrap(session)
    return (session.scalars(select(ChatRunEvent).where(ChatRunEvent.run_id == run_id, ChatRunEvent.sequence > after)
                                     .order_by(ChatRunEvent.sequence).limit(100)).all())


def lock_expired_cancellations(now, kind, session):
    session = unwrap(session)
    return (session.scalars(select(ChatRun).where(ChatRun.execution_kind == kind, ChatRun.state == "cancelling", or_(
            ChatRun.lease_expires_at <= now, ChatRun.owner_token.is_(None)))
            .limit(10).with_for_update(skip_locked=True)).all())


def lock_expired_waiting_runs(now, kind, session):
    session = unwrap(session)
    return (session.scalars(select(ChatRun).where(ChatRun.execution_kind == kind, ChatRun.state == "waiting", ChatRun.expires_at <= now)
                .limit(10).with_for_update(skip_locked=True)).all())


def lock_next_run(now, run_id, kind, session):
    session = unwrap(session)
    return (session.scalar(select(ChatRun).where(ChatRun.execution_kind == kind,
                ChatRun.id == run_id if run_id is not None else True, or_(and_(ChatRun.state == "queued", or_(
                ChatRun.lease_expires_at.is_(None), ChatRun.lease_expires_at <= now)), and_(
                ChatRun.state == "running", ChatRun.generation > 0, ChatRun.lease_expires_at <= now)))
                .order_by(ChatRun.created_at, ChatRun.id).limit(1).with_for_update(skip_locked=True)))


def operation_count(claim, session):
    session = unwrap(session)
    return (session.scalar(select(func.count()).select_from(ChatRunOperation).where(ChatRunOperation.run_id == claim.run_id)))


def operation_artifacts(key, run, session):
    session = unwrap(session)
    return (list(session.scalars(select(RunArtifact).where(
                    RunArtifact.run_id == run.id, RunArtifact.operation_key == key).order_by(RunArtifact.name))))


def lock_pending_events(limit, session):
    session = unwrap(session)
    return (session.scalars(select(ChatEventOutbox).where(ChatEventOutbox.dispatched_at.is_(None))
                .order_by(ChatEventOutbox.run_id, ChatEventOutbox.sequence).limit(limit).with_for_update(skip_locked=True)).all())


def supports_notifications(session):
    session = unwrap(session)
    return (session.get_bind().dialect.name == "postgresql")


def notify_event(row, session):
    session = unwrap(session)
    return (session.execute(select(func.pg_notify("rag_run_events", json.dumps({"run_id": str(row.run_id), "sequence": row.sequence})))))
