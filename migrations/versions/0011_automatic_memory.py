"""Opt-in personal automatic memory and durable synchronization receipts."""
from alembic import op
import sqlalchemy as sa

revision = '0011_automatic_memory'
down_revision = '0010_run_artifacts'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('memory_settings',
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('rag.users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False), sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('enabled_since', sa.DateTime(timezone=True)), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('generation >= 1'), schema='rag')
    op.create_table('automatic_memories',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('rag.users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False), sa.Column('uri', sa.String(1024), nullable=False),
        sa.Column('content', sa.Text(), nullable=False), sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('source_session_id', sa.Uuid()), sa.Column('source_message_id', sa.Uuid()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'generation', 'uri'),
        sa.CheckConstraint("state IN ('ready','deleting','deleted')"), schema='rag')
    op.create_index('ix_automatic_memory_owner', 'automatic_memories', ['user_id', 'generation', 'state'], schema='rag')
    op.create_table('memory_sync_jobs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('rag.users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False), sa.Column('dedup_key', sa.String(128), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('receipt', sa.JSON(), nullable=False), sa.Column('owner_token', sa.Uuid()),
        sa.Column('lease_until', sa.DateTime(timezone=True)), sa.Column('available_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('error_code', sa.String(80)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False), sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'generation', 'dedup_key'),
        sa.CheckConstraint("kind IN ('capture','delete','purge')"),
        sa.CheckConstraint("state IN ('pending','running','waiting','succeeded','failed','uncertain','cancelled')"), schema='rag')
    op.create_index('ix_memory_sync_poll', 'memory_sync_jobs', ['state', 'available_at'], schema='rag')


def downgrade():
    raise RuntimeError('Memory deletion receipts retain audit history; restore a verified backup instead')
