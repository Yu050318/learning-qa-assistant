"""Persistence queries for app/service/sandbox.py."""
from sqlalchemy import select
from app.entity.do.models import ChatRun
from app.dao.postgres.transactions import unwrap


def get_run(run_id, session):
    session = unwrap(session)
    return (session.scalar(select(ChatRun).where(ChatRun.id==run_id).with_for_update(nowait=True)))
