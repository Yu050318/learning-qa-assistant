"""Persist version index manifests without rewriting source files or vectors."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_document_publication"
down_revision = "0006_evaluation_workbench"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("organizations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True), schema="rag")
    op.add_column("chat_sessions", sa.Column("organization_id", sa.Uuid(), nullable=True), schema="rag")
    op.create_foreign_key("fk_chat_sessions_workspace", "chat_sessions", "organizations", ["organization_id"], ["id"], source_schema="rag", referent_schema="rag")
    op.add_column("documents", sa.Column("organization_id", sa.Uuid(), nullable=True), schema="rag")
    op.create_foreign_key("fk_documents_workspace", "documents", "organizations", ["organization_id"], ["id"], source_schema="rag", referent_schema="rag")
    constraints = [row for row in sa.inspect(op.get_bind()).get_unique_constraints("documents", schema="rag")
                   if set(row["column_names"]) == {"user_id", "content_hash"}]
    if len(constraints) != 1 or not constraints[0]["name"]:
        raise ValueError("Expected the existing document source uniqueness constraint")
    checks = [row for row in sa.inspect(op.get_bind()).get_check_constraints("documents", schema="rag") if "status" in row["sqltext"]]
    if len(checks) != 1 or not checks[0]["name"]:
        raise ValueError("Expected the existing document status constraint")
    op.add_column("documents", sa.Column("build_fingerprint", sa.String(64), nullable=False, server_default="legacy"), schema="rag")
    op.drop_constraint(constraints[0]["name"], "documents", schema="rag", type_="unique")
    op.create_index("uq_documents_source_build", "documents", ["user_id", "content_hash", "build_fingerprint"], unique=True,
                    postgresql_where=sa.text("status != 'retired' AND organization_id IS NULL"), schema="rag")
    op.create_index("uq_documents_workspace_source_build", "documents", ["user_id", "organization_id", "content_hash", "build_fingerprint"], unique=True,
                    postgresql_where=sa.text("status != 'retired' AND organization_id IS NOT NULL"), schema="rag")
    op.drop_constraint(checks[0]["name"], "documents", schema="rag", type_="check")
    op.create_check_constraint("ck_documents_status", "documents", "status IN ('processing','ready','failed','deleting','retired')", schema="rag")
    op.create_table("document_indexes",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("rag.documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("collection_name", sa.String(160), nullable=False),
        sa.Column("database_name", sa.String(128), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("connection_sha256", sa.String(64), nullable=False),
        sa.Column("build_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("leaf_sha256", sa.String(64), nullable=True),
        sa.Column("leaf_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(16), nullable=False, server_default="building"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('building','verified','published','superseded','retiring','cleaned','failed')", name="ck_document_indexes_state"),
        sa.CheckConstraint("leaf_count >= 0 AND embedding_dimension >= 64", name="ck_document_indexes_bounds"), schema="rag")
    op.create_table("document_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("rag.documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("rag.users.id"), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("rag.organizations.id"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("processed_units", sa.Integer(), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('ingest','hierarchy','delete')", name="ck_document_tasks_kind"),
        sa.CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')", name="ck_document_tasks_state"), schema="rag")
    op.create_index("ix_document_tasks_owner_created", "document_tasks", ["user_id", "created_at"], schema="rag")
    op.add_column("ingestion_jobs", sa.Column("task_id", sa.Uuid(), nullable=True), schema="rag")
    op.create_foreign_key("fk_ingestion_jobs_task", "ingestion_jobs", "document_tasks", ["task_id"], ["id"], source_schema="rag", referent_schema="rag")
    op.drop_constraint("ck_ingestion_jobs_kind", "ingestion_jobs", schema="rag", type_="check")
    op.create_check_constraint("ck_ingestion_jobs_kind", "ingestion_jobs", "kind IN ('ingest','hierarchy','delete')", schema="rag")
    op.execute(sa.text("""INSERT INTO rag.document_tasks (id,document_id,user_id,original_name,kind,state,phase,processed_units,total_units,attempts,max_attempts,error_code,details,created_at,updated_at)
        SELECT j.document_id,j.document_id,j.user_id,d.original_name,j.kind,j.state,'legacy',0,NULL,j.attempts,j.max_attempts,j.last_error_code,jsonb_build_object('imported_legacy',true),j.created_at,j.updated_at
        FROM rag.ingestion_jobs j JOIN rag.documents d ON d.id=j.document_id"""))
    op.execute(sa.text("UPDATE rag.ingestion_jobs SET task_id=document_id"))
    op.create_table("run_index_pins",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("rag.documents.id"), primary_key=True), schema="rag")
    op.create_index("ix_run_index_pins_document", "run_index_pins", ["document_id"], schema="rag")
    op.add_column("knowledge_documents", sa.Column("series_id", sa.Uuid(), nullable=True), schema="rag")
    op.add_column("knowledge_documents", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()), schema="rag")
    op.execute(sa.text("""UPDATE rag.knowledge_documents k SET series_id=COALESCE(
        (SELECT v.series_id FROM rag.document_versions v WHERE v.document_id=k.document_id), k.document_id)"""))
    # One explicit binding is authoritative. With multiple historical bindings,
    # retain the old effective current only when it was actually shared; do not
    # guess a new public version if the private pointer names another document.
    op.execute(sa.text("""UPDATE rag.knowledge_documents k SET is_current=(
        (SELECT count(*) FROM rag.knowledge_documents other WHERE other.knowledge_base_id=k.knowledge_base_id
            AND other.series_id=k.series_id)=1 OR EXISTS (
        SELECT 1 FROM rag.document_series s WHERE s.id=k.series_id AND s.current_document_id=k.document_id))"""))
    op.alter_column("knowledge_documents", "series_id", nullable=False, schema="rag")
    op.create_index("uq_knowledge_documents_series_current", "knowledge_documents", ["knowledge_base_id", "series_id"],
                    unique=True, postgresql_where=sa.text("is_current"), schema="rag")


def downgrade():
    raise RuntimeError("Document publication downgrade could erase verified version identities; restore a verified backup instead")
