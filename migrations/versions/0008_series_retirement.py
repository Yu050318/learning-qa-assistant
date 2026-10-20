"""Close version families and retain parent/child cleanup task receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0008_series_retirement"
down_revision = "0007_document_publication"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_tasks", sa.Column("parent_task_id", sa.Uuid(), nullable=True), schema="rag")
    op.create_foreign_key("fk_document_tasks_parent", "document_tasks", "document_tasks", ["parent_task_id"], ["id"], source_schema="rag", referent_schema="rag")
    op.create_index("ix_document_tasks_parent", "document_tasks", ["parent_task_id"], schema="rag")
    op.drop_constraint("ck_document_tasks_kind", "document_tasks", schema="rag", type_="check")
    op.create_check_constraint("ck_document_tasks_kind", "document_tasks", "kind IN ('ingest','hierarchy','delete','delete_series')", schema="rag")
    op.add_column("document_series", sa.Column("retirement_task_id", sa.Uuid(), nullable=True), schema="rag")
    op.create_foreign_key("fk_document_series_retirement", "document_series", "document_tasks", ["retirement_task_id"], ["id"], source_schema="rag", referent_schema="rag")


def downgrade():
    raise RuntimeError("Series retirement retains cleanup history; restore a verified backup instead")
