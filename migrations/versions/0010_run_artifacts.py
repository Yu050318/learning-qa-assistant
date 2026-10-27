"""Run-owned bounded artifacts, visible only after the tool receipt commits."""
from alembic import op
import sqlalchemy as sa

revision = "0010_run_artifacts"
down_revision = "0009_capability_control"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("run_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("rag.users.id"), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("rag.chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("rag.organizations.id"), nullable=True),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("preview_kind", sa.String(12), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id", "operation_key"],
            ["rag.chat_run_operations.run_id", "rag.chat_run_operations.operation_key"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "operation_key", "name", name="uq_run_artifacts_operation_name"),
        sa.CheckConstraint("size_bytes >= 0 AND size_bytes <= 2097152 AND length(content) = size_bytes", name="ck_run_artifacts_size"),
        sa.CheckConstraint("tool_version >= 1 AND length(sha256) = 64", name="ck_run_artifacts_identity"),
        sa.CheckConstraint("preview_kind IN ('text','image','none')", name="ck_run_artifacts_preview"), schema="rag")
    op.create_index("ix_run_artifacts_owner_run", "run_artifacts", ["user_id", "run_id"], schema="rag")


def downgrade():
    raise RuntimeError("Artifact receipts retain audit history; restore a verified backup instead")
