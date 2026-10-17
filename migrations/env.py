"""Use the application's validated database configuration; never print DSNs."""
from alembic import context

from app.core.config import get_settings
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, include_schemas=True)
    with context.begin_transaction():
        # Serialize upgrades from multiple deployment processes.
        connection.exec_driver_sql("SELECT pg_advisory_xact_lock(723984251)")
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Schema adoption requires an online structural check; offline SQL is not supported")

connection = context.config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    database = Database(get_settings())
    try:
        with database.engine.connect() as connection:
            migrate(connection)
    finally:
        database.close()
