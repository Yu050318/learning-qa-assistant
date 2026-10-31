"""Explicit schema upgrade and one-time legacy model-admin migration."""
import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.core.config import get_settings
from app.entity.do.models import User
from app.dao.postgres.schema_migrations import upgrade


def migrate_model_admins(database, usernames):
    """Explicit opt-in only: changing the whitelist never grants runtime access."""
    names = {name.strip().lower() for name in usernames if name.strip()}
    with database.sessions() as session:
        users = session.scalars(select(User).where(User.username.in_(names)).with_for_update()).all() if names else []
        if {user.username for user in users} != names or any(not user.is_active for user in users):
            raise ValueError("Legacy administrator list contains unknown or inactive accounts; no roles changed")
        changed = sum(user.role != "admin" for user in users)
        for user in users:
            user.role = "admin"
        session.commit()
        return changed


def main():
    from app.dao.postgres.database import Database
    parser = argparse.ArgumentParser(description="显式升级数据库；不会修改 .env 或自动提升用户")
    parser.add_argument("--model-admins", action="store_true", help="升级后显式将旧模型管理员白名单转为平台管理员")
    options = parser.parse_args()
    settings = get_settings()
    database = Database(settings)
    try:
        upgrade(database)
        print("Database schema upgraded to head")
        if options.model_admins:
            print(f"Legacy administrator assignments migrated: {migrate_model_admins(database, settings.model_admin_usernames)}")
        return 0
    except Exception as error:
        # SQLAlchemy errors may contain connection details; never print raw exceptions.
        print(f"Migration failed: {type(error).__name__}; inspect schema compatibility and configuration")
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
