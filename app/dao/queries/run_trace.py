"""Persistence queries for app/service/run_trace.py."""
from sqlalchemy import select
from app.entity.do.models import ChatMessage, ChatRunCheckpoint, ChatRunOperation
from app.dao.postgres.transactions import unwrap


def knowledge_tool_receipts(run, session):
    session = unwrap(session)
    return (session.execute(select(ChatRunOperation.result,ChatRunOperation.updated_at).where(
        ChatRunOperation.run_id==run.id,ChatRunOperation.operation_key.like('tool/%'),
        ChatRunOperation.state=='succeeded').order_by(ChatRunOperation.updated_at.desc()).limit(4)))
