"""Persistent platform roles; existing accounts remain ordinary users."""
from alembic import op
import sqlalchemy as sa

revision = "0002_platform_roles"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("role", sa.String(12), nullable=False, server_default="user"), schema="rag")
    op.create_check_constraint("ck_users_platform_role", "users", "role IN ('user', 'admin')", schema="rag")


def downgrade():
    raise RuntimeError("Removing roles would discard administrator assignments; restore a verified backup")
