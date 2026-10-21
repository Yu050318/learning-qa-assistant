"""Versioned capability declarations; existing documents and runs are unchanged."""
from alembic import op
import json
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_capability_control"
down_revision = "0008_series_retirement"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("capability_configuration",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("configuration", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1 AND revision >= 1", name="ck_capability_configuration_revision"), schema="rag")
    op.create_table("capability_configuration_versions",
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("configuration", JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("change", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_capability_configuration_versions_revision"), schema="rag")
    # Frozen initial defaults: migrations must not import a future runtime catalog.
    skills = {}
    for name, description, instructions, tool in (
        ("knowledge", "知识库问答", "仅从当前授权知识库检索，使用真实引用，缺少证据时说明。", "delegate_rag"),
        ("sql-assistant", "只读数据查询", "使用只读 SQL 工具核对结构与数据，遵守现有表白名单。", "delegate_sql"),
        ("web-research", "公开资料检索", "仅使用已授权的公开查询词和网页，不传输私有资料。", "delegate_web"),
    ):
        skills[name] = {"version": 1, "source": "builtin", "declaration": {"name": name,
            "description": description, "instructions": instructions, "allowed_tools": [tool],
            "required_roles": [], "required_secrets": [], "enabled": True}}
    configuration = {"schema_version": 1, "skills": skills, "tools": {}, "retired_skills": [], "retired_tools": [], "sql": None, "web": None}
    op.get_bind().execute(sa.text("INSERT INTO rag.capability_configuration VALUES (1,1,CAST(:config AS jsonb),now())"), {"config": json.dumps(configuration, ensure_ascii=False)})
    op.get_bind().execute(sa.text("INSERT INTO rag.capability_configuration_versions VALUES (1,CAST(:config AS jsonb),NULL,CAST(:change AS jsonb),now())"),
        {"config": json.dumps(configuration, ensure_ascii=False), "change": '{"operation":"initialize"}'})


def downgrade():
    raise RuntimeError("Capability versions retain audit history; restore a verified backup instead")
