"""Durable evaluation datasets/jobs/cases and isolated evaluation Run queues."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_evaluation_workbench"
down_revision = "0005_model_catalog_history"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_sessions", sa.Column("kind", sa.String(12), nullable=False, server_default="chat"), schema="rag")
    op.create_check_constraint("ck_chat_sessions_kind", "chat_sessions", "kind IN ('chat','evaluation')", schema="rag")
    op.add_column("chat_runs", sa.Column("execution_kind", sa.String(12), nullable=False, server_default="chat"), schema="rag")
    op.add_column("chat_runs", sa.Column("result_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"), schema="rag")
    op.create_check_constraint("ck_chat_runs_kind", "chat_runs", "execution_kind IN ('chat','evaluation')", schema="rag")
    op.create_table("evaluation_datasets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("rag.users.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("created_by", "fingerprint"),
        sa.CheckConstraint("case_count >= 1 AND case_count <= 1000"), schema="rag")
    op.create_table("evaluation_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("rag.users.id"), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("rag.evaluation_datasets.id"), nullable=False),
        sa.Column("baseline_job_id", sa.Uuid(), sa.ForeignKey("rag.evaluation_jobs.id"), nullable=True),
        sa.Column("client_key", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(64), nullable=False),
        sa.Column("gate_policy", postgresql.JSONB(), nullable=False),
        sa.Column("split", sa.String(12), nullable=False),
        sa.Column("profile", sa.String(12), nullable=False),
        sa.Column("model_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("scope_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("completed_cases", sa.Integer(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("report", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("created_by", "client_key"),
        sa.CheckConstraint("state IN ('queued','running','cancelling','cancelled','succeeded','failed')", name="ck_evaluation_jobs_state"),
        sa.CheckConstraint("completed_cases >= 0 AND completed_cases <= total_cases AND total_cases >= 1", name="ck_evaluation_jobs_progress"),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1 AND generation >= 0", name="ck_evaluation_jobs_attempts"), schema="rag")
    op.create_index("ix_evaluation_jobs_claim", "evaluation_jobs", ["state", "available_at", "lease_expires_at"], schema="rag")
    op.create_table("evaluation_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("rag.evaluation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_id", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("label", postgresql.JSONB(), nullable=False),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("rag.chat_sessions.id"), nullable=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag.chat_runs.id"), nullable=True, unique=True),
        sa.Column("prediction", postgresql.JSONB(), nullable=True),
        sa.Column("review", postgresql.JSONB(), nullable=True),
        sa.Column("judge_receipt", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id", "case_id"), sa.UniqueConstraint("job_id", "position"),
        sa.CheckConstraint("state IN ('queued','running','succeeded','failed','cancelled')", name="ck_evaluation_cases_state"),
        sa.CheckConstraint("position >= 0", name="ck_evaluation_cases_position"), schema="rag")


def downgrade():
    raise RuntimeError("Evaluation downgrade would erase datasets and results; restore a verified backup")
