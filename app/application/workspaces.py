"""Personal conversations and organization spaces share existing membership ACLs."""
from sqlalchemy import func, select

from app.core.errors import AppError
from app.infrastructure.persistence.models import ChatSession, Organization, OrganizationMember


def list_workspaces(settings, database, user_id):
    if not settings.workspaces_enabled:
        raise AppError("WORKSPACES_DISABLED", "团队空间尚未启用", 503)
    with database.sessions() as session:
        counts = dict(session.execute(select(ChatSession.organization_id, func.count()).where(
            ChatSession.user_id == user_id, ChatSession.kind == "chat").group_by(ChatSession.organization_id)).all())
        organizations = session.execute(select(Organization, OrganizationMember).join(OrganizationMember).where(
            OrganizationMember.user_id == user_id, Organization.archived_at.is_(None)).order_by(Organization.name, Organization.id)).all()
        return [{"id": None, "kind": "personal", "name": "个人空间", "role": "owner", "session_count": counts.get(None, 0)},
            *[{"id": org.id, "kind": "team", "name": org.name, "role": member.role, "session_count": counts.get(org.id, 0)}
              for org, member in organizations]]
