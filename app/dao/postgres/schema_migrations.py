"""Explicit schema upgrade, shared by the CLI and database initialization."""
from alembic import command
from alembic.config import Config

from app.core.config import PROJECT_ROOT


def upgrade(database, revision='head'):
    config = Config(str(PROJECT_ROOT / 'alembic.ini'))
    with database.engine.begin() as connection:
        config.attributes['connection'] = connection
        command.upgrade(config, revision)
