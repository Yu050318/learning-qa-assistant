"""Preserve each model configuration revision, including the pre-upgrade catalog."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_model_catalog_history"
down_revision = "0004_durable_run_control"
branch_labels = None
depends_on = None


def upgrade():
    for column in [sa.Column("model_role", sa.String(20), nullable=True),
                   sa.Column("model_profile_id", sa.String(64), nullable=True),
                   sa.Column("model_profile_version", sa.Integer(), nullable=True)]:
        op.add_column("model_calls", column, schema="rag")
    op.create_check_constraint("ck_model_calls_profile", "model_calls",
        "(model_role IS NULL AND model_profile_id IS NULL AND model_profile_version IS NULL) OR "
        "(model_role IS NOT NULL AND model_role IN ('answer','fast','grader','evaluator') AND "
        "model_profile_id IS NOT NULL AND model_profile_version IS NOT NULL AND model_profile_version >= 1)", schema="rag")
    op.create_table("model_configuration_versions",
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_model_configuration_versions_revision"), schema="rag")
    op.execute("""INSERT INTO rag.model_configuration_versions(revision,configuration,created_by,created_at)
                  SELECT revision,configuration,NULL,updated_at FROM rag.model_configuration""")


def downgrade():
    raise RuntimeError("Model catalog downgrade would erase version history; restore a verified backup")
