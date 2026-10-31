"""Persistence queries for app/service/workspaces.py."""
from sqlalchemy import func, select
from app.entity.do.models import ChatSession, Organization, OrganizationMember
from app.dao.postgres.transactions import unwrap


def session_counts(session, user_id):
    session = unwrap(session)
    return (dict(session.execute(select(ChatSession.organization_id, func.count()).where(
            ChatSession.user_id == user_id, ChatSession.kind == "chat").group_by(ChatSession.organization_id)).all()))


def user_organizations(session, user_id):
    session = unwrap(session)
    return (session.execute(select(Organization, OrganizationMember).join(OrganizationMember).where(
            OrganizationMember.user_id == user_id, Organization.archived_at.is_(None)).order_by(Organization.name, Organization.id)).all())
