"""Run ownership, checkpoints, operation receipts and transactional event outbox."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_durable_run_control"
down_revision = "0003_worker_readiness"
branch_labels = None
depends_on = None


def upgrade():
    for check in sa.inspect(op.get_bind()).get_check_constraints("chat_runs", schema="rag"):
        if "state" in check["sqltext"]:
            op.drop_constraint(check["name"], "chat_runs", schema="rag", type_="check")
    op.create_check_constraint("ck_chat_runs_state", "chat_runs",
        "state IN ('queued', 'running', 'waiting', 'cancelling', 'cancelled', 'succeeded', 'failed', 'interrupted')", schema="rag")
    op.drop_index("ix_chat_runs_session_active", table_name="chat_runs", schema="rag")
    op.create_index("ix_chat_runs_session_active", "chat_runs", ["session_id"], unique=True, schema="rag",
                    postgresql_where=sa.text("state IN ('queued', 'running', 'waiting', 'cancelling')"))
    for column in [
        sa.Column("user_message_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("execution_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("owner_token", sa.Uuid(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remaining_seconds", sa.Float(), nullable=True),
        sa.Column("active_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_key", sa.Uuid(), nullable=True),
    ]:
        op.add_column("chat_runs", column, schema="rag")
    op.create_foreign_key("fk_chat_runs_user_message", "chat_runs", "chat_messages", ["user_message_id"], ["id"],
                          source_schema="rag", referent_schema="rag", ondelete="SET NULL")
    op.create_index("ix_chat_runs_claim", "chat_runs", ["state", "lease_expires_at", "created_at"], schema="rag")
    op.create_table("chat_run_checkpoints",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1"), schema="rag")
    op.create_table("chat_run_operations",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("rag.chat_runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operation_key", sa.String(128), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("readonly", sa.Boolean(), nullable=False),
        sa.Column("owner_generation", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('started', 'succeeded', 'uncertain')"), schema="rag")
    op.create_table("chat_event_outbox",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id", "sequence"], ["rag.chat_run_events.run_id", "rag.chat_run_events.sequence"], ondelete="CASCADE"), schema="rag")
    op.create_index("ix_chat_event_outbox_pending", "chat_event_outbox", ["run_id", "sequence"], schema="rag",
                    postgresql_where=sa.text("dispatched_at IS NULL"))
    # Historical events remain replayable. Legacy executions have no checkpoints
    # and must never be silently restarted by a new worker.
    op.execute(sa.text("""
        WITH interrupted AS (
            UPDATE rag.chat_runs SET state='interrupted', sequence=sequence+1, event_bytes=event_bytes+512
            WHERE state='running' RETURNING id, sequence
        )
        INSERT INTO rag.chat_run_events (run_id,sequence,event,payload)
        SELECT id,sequence,'error',jsonb_build_object('run_id',id::text,'sequence',sequence,
            'code','RUN_UPGRADED_WITHOUT_CHECKPOINT','message','旧执行没有检查点，请重新提问','status',409)
        FROM interrupted
    """))


def downgrade():
    raise RuntimeError("Run control downgrade would discard checkpoints; restore a verified backup")
