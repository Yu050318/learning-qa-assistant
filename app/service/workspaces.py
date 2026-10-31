"""Personal conversations and organization spaces share existing membership ACLs."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import workspaces as queries


from app.core.errors import AppError
from app.entity.do.models import ChatSession, Organization, OrganizationMember


def list_workspaces(settings, database, user_id):
    if not settings.workspaces_enabled:
        raise AppError("WORKSPACES_DISABLED", "团队空间尚未启用", 503)
    with transaction_scope(database) as session:
        counts = queries.session_counts(session, user_id)
        organizations = queries.user_organizations(session, user_id)
        return [{"id": None, "kind": "personal", "name": "个人空间", "role": "owner", "session_count": counts.get(None, 0)},
            *[{"id": org.id, "kind": "team", "name": org.name, "role": member.role, "session_count": counts.get(org.id, 0)}
              for org, member in organizations]]
