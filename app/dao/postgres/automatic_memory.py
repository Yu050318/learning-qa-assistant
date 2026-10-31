"""All SQL for personal automatic memory; caller owns the transaction."""
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, and_
from sqlalchemy.orm import aliased

from app.dao.postgres.transactions import unwrap
from app.entity.do.models import AutomaticMemory, ChatMessage, ChatSession, MemorySettings, MemorySyncJob, User, utcnow
from app.entity.time import aware


def settings(transaction, user_id, *, create=False, lock=False):
    session = unwrap(transaction)
    if create:
        # Serialize the first insert as well as subsequent consent updates.
        session.scalar(select(User.id).where(User.id == user_id).with_for_update())
    query = select(MemorySettings).where(MemorySettings.user_id == user_id)
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None and create:
        row = MemorySettings(user_id=user_id, enabled=False, generation=1)
        session.add(row)
        session.flush()
    return row


def memories(transaction, user_id, generation=None, *, include_deleted=False):
    query = select(AutomaticMemory).where(AutomaticMemory.user_id == user_id)
    if generation is not None:
        query = query.where(AutomaticMemory.generation == generation)
    if not include_deleted:
        query = query.where(AutomaticMemory.state == "ready")
    return list(unwrap(transaction).scalars(query.order_by(AutomaticMemory.updated_at.desc(), AutomaticMemory.id)))


def memory(transaction, user_id, memory_id):
    return unwrap(transaction).scalar(select(AutomaticMemory).where(
        AutomaticMemory.user_id == user_id, AutomaticMemory.id == memory_id).with_for_update())


def source_times(transaction, user_id, message_ids):
    return dict(unwrap(transaction).execute(select(ChatMessage.id, ChatMessage.created_at).where(
        ChatMessage.user_id == user_id, ChatMessage.id.in_(message_ids))).all())


def jobs(transaction, user_id):
    return list(unwrap(transaction).scalars(select(MemorySyncJob).where(MemorySyncJob.user_id == user_id)
        .order_by(MemorySyncJob.created_at.desc()).limit(100)))


def captures(transaction, user_id, generation):
    return list(unwrap(transaction).scalars(select(MemorySyncJob).where(
        MemorySyncJob.user_id == user_id, MemorySyncJob.generation == generation,
        MemorySyncJob.kind == 'capture').order_by(MemorySyncJob.created_at, MemorySyncJob.id)))


def mark_source_removed(transaction, capture):
    row = unwrap(transaction).scalar(select(MemorySyncJob).where(
        MemorySyncJob.id == capture.id, MemorySyncJob.user_id == capture.user_id,
        MemorySyncJob.generation == capture.generation, MemorySyncJob.kind == 'capture').with_for_update())
    row.receipt = {**row.receipt, 'source_removed': True}


def job_counts(transaction, user_id, generation):
    rows = unwrap(transaction).execute(select(MemorySyncJob.state, MemorySyncJob.kind,
        MemorySyncJob.generation, func.count()).where(MemorySyncJob.user_id == user_id)
        .group_by(MemorySyncJob.state, MemorySyncJob.kind, MemorySyncJob.generation)).all()
    return (sum(count for state, _, _, count in rows if state in {'pending', 'running', 'waiting'}),
            sum(count for state, kind, epoch, count in rows if state in {'failed', 'uncertain'}
                and (epoch == generation or kind != 'capture')))


def now(transaction):
    session = unwrap(transaction)
    return aware(session.scalar(select(func.clock_timestamp()))) if session.get_bind().dialect.name == 'postgresql' else utcnow()


def enqueue(transaction, user_id, generation, kind, dedup_key, receipt):
    session = unwrap(transaction)
    row = session.scalar(select(MemorySyncJob).where(MemorySyncJob.user_id == user_id,
        MemorySyncJob.generation == generation, MemorySyncJob.dedup_key == dedup_key))
    if row is None:
        row = MemorySyncJob(user_id=user_id, generation=generation, kind=kind,
                            dedup_key=dedup_key, receipt=receipt)
        session.add(row)
    return row


def enqueue_capture(transaction, context, assistant_id):
    """Called in the same commit as the successful assistant message."""
    session = unwrap(transaction)
    consent = settings(session, context.user_id, lock=True)
    if not consent or not consent.enabled or context.organization_id is not None or context.shared_only:
        return
    parent = session.get(ChatSession, context.session_id)
    source = session.get(ChatMessage, context.source_message_id)
    if (parent is None or parent.user_id != context.user_id or parent.kind != "chat"
            or parent.organization_id is not None or source is None or source.user_id != context.user_id
            or source.session_id != parent.id or source.role != "user"
            or not consent.enabled_since or aware(source.created_at) < aware(consent.enabled_since)):
        return
    enqueue(session, context.user_id, consent.generation, "capture", str(source.id),
            {"session_id": str(parent.id), "source_message_id": str(source.id), "assistant_id": str(assistant_id), "phase": "new"})


def capture_source(transaction, job):
    from uuid import UUID
    session = unwrap(transaction)
    source = session.get(ChatMessage, UUID(job.receipt["source_message_id"]))
    assistant = session.get(ChatMessage, UUID(job.receipt["assistant_id"]))
    parent = session.get(ChatSession, UUID(job.receipt["session_id"]))
    if (source is None or assistant is None or parent is None or parent.kind != "chat" or parent.organization_id is not None
            or source.user_id != job.user_id or assistant.user_id != job.user_id or parent.user_id != job.user_id
            or source.session_id != parent.id or assistant.session_id != parent.id
            or source.role != "user" or assistant.role != "assistant" or not assistant.content):
        return None
    return source.content


def claim(transaction):
    session = unwrap(transaction)
    instant = now(session)
    older = aliased(MemorySyncJob)
    preceding = select(older.id).where(older.user_id == MemorySyncJob.user_id,
        older.generation == MemorySyncJob.generation, older.state.in_(("pending", "waiting", "running")),
        or_(older.created_at < MemorySyncJob.created_at,
            and_(older.created_at == MemorySyncJob.created_at, older.id < MemorySyncJob.id))).exists()
    row = session.scalar(select(MemorySyncJob).where(
        MemorySyncJob.state.in_(("pending", "waiting", "running")), MemorySyncJob.available_at <= instant,
        or_(MemorySyncJob.lease_until.is_(None), MemorySyncJob.lease_until < instant), ~preceding)
        .order_by(MemorySyncJob.created_at, MemorySyncJob.id).with_for_update(skip_locked=True).limit(1))
    if row:
        row.owner_token, row.lease_until, row.state = uuid4(), instant + timedelta(seconds=180), "running"
    return row


def owned(transaction, job):
    row = unwrap(transaction).scalar(select(MemorySyncJob).where(MemorySyncJob.id == job.id,
        MemorySyncJob.owner_token == job.owner_token, MemorySyncJob.state == "running").with_for_update())
    if row is None or row.lease_until is None or aware(row.lease_until) <= now(transaction):
        from app.core.errors import AppError
        raise AppError("MEMORY_LEASE_LOST", "记忆同步租约已失效", 409)
    return row
