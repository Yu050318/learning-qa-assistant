"""Owner-scoped message and Run reads used by chat services."""
from sqlalchemy import select

from app.dao.postgres.transactions import unwrap
from app.entity.do.models import ChatMessage, ChatRun


def message(transaction, user_id, message_id):
    return unwrap(transaction).scalar(select(ChatMessage).where(
        ChatMessage.id == message_id, ChatMessage.user_id == user_id))


def recent_run_ids(transaction, user_id, session_id):
    return list(unwrap(transaction).scalars(select(ChatRun.id).where(
        ChatRun.session_id == session_id, ChatRun.user_id == user_id)
        .order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).limit(20)))
