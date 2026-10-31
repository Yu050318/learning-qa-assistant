"""Persistence queries for app/service/artifacts.py."""
from sqlalchemy import func, select
from sqlalchemy.orm import load_only
from app.entity.do.models import ChatRunOperation, RunArtifact
from app.entity.bo.capabilities import SANDBOX_TOOL
from app.dao.postgres.transactions import unwrap


def operation_artifacts(operation_key, run, session):
    session = unwrap(session)
    return (list(session.scalars(select(RunArtifact).where(
                RunArtifact.run_id == run.id, RunArtifact.operation_key == operation_key).order_by(RunArtifact.name))))


def run_totals(run, session):
    session = unwrap(session)
    return (session.execute(select(func.count(), func.coalesce(func.sum(RunArtifact.size_bytes), 0))
                .where(RunArtifact.run_id == run.id)).one())


def _visible_artifacts(parent, run, version):
    return (select(RunArtifact).join(ChatRunOperation,
            (ChatRunOperation.run_id == RunArtifact.run_id) &
            (ChatRunOperation.operation_key == RunArtifact.operation_key)).where(
                RunArtifact.run_id == run.id, RunArtifact.user_id == run.user_id,
                RunArtifact.session_id == run.session_id,
                RunArtifact.organization_id == parent.organization_id,
                RunArtifact.tool_name == SANDBOX_TOOL, RunArtifact.tool_version == version,
                ChatRunOperation.state == 'succeeded'))


def list_visible_metadata(METADATA_FIELDS, parent, run, session, version):
    session = unwrap(session)
    return list(session.scalars(_visible_artifacts(parent, run, version).options(load_only(
                *(getattr(RunArtifact, name) for name in METADATA_FIELDS))).order_by(RunArtifact.created_at, RunArtifact.id)))


def get_visible_artifact(artifact_id, parent, run, session, version):
    session = unwrap(session)
    return (session.scalar(_visible_artifacts(parent, run, version).where(RunArtifact.id == artifact_id)))
