"""Worker pool heartbeats and durable hierarchy backfill on the existing queue."""
from alembic import op
import sqlalchemy as sa

revision = "0003_worker_readiness"
down_revision = "0002_platform_roles"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingestion_jobs", sa.Column("kind", sa.String(16), nullable=False, server_default="ingest"), schema="rag")
    op.create_check_constraint("ck_ingestion_jobs_kind", "ingestion_jobs", "kind IN ('ingest', 'hierarchy')", schema="rag")
    op.create_table("worker_heartbeats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False), schema="rag")


def downgrade():
    raise RuntimeError("Worker queue downgrade would discard task types; restore a verified backup")
