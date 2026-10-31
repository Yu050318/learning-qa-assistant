"""Platform administration never grants access to another user's content."""
from app.manager.transactions import transaction_scope, as_transaction
from app.core.errors import AppError
from app.entity.do.models import User


def is_platform_admin(database, user_id):
    with transaction_scope(database) as session:
        user = as_transaction(session).load(User, user_id)
        return bool(user and user.is_active and user.role == "admin")


def require_platform_admin(database, user_id):
    if not is_platform_admin(database, user_id):
        raise AppError("FORBIDDEN", "需要平台管理员权限", 403)
