"""Persistence queries for app/manager/run_checkpoint.py."""
from sqlalchemy import func, select
from app.entity.do.models import ChatRunOperation
from app.dao.postgres.transactions import unwrap


def model_attempts(run_id, session):
    session = unwrap(session)
    return (session.scalar(select(func.coalesce(func.sum(ChatRunOperation.attempts), 0)).where(
                ChatRunOperation.run_id == run_id, ChatRunOperation.operation_key.like("model/%"))))
