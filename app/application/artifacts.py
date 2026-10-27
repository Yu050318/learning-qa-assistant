"""Fenced, Run-owned artifact storage. Pending tool results are never public."""
from sqlalchemy import func, select
from sqlalchemy.orm import load_only
from app.application.ingestion_jobs import aware

from app.core.errors import AppError, not_found
from app.domain.artifacts import classify_artifact, render_artifact_preview, verify_artifact
from app.domain.capabilities import SANDBOX_TOOL
from app.domain.sandbox import SandboxFile
from app.infrastructure.persistence.models import ChatRunOperation, RunArtifact
from app.infrastructure.persistence.repository import Repository

MAX_RUN_FILES = 64
MAX_RUN_BYTES = 16 * 1024 * 1024
METADATA_FIELDS = ('id', 'run_id', 'user_id', 'session_id', 'organization_id',
    'tool_name', 'tool_version', 'name', 'size_bytes', 'sha256', 'media_type', 'preview_kind', 'created_at')


class ArtifactService:
    def __init__(self, store, capabilities):
        self.store, self.capabilities = store, capabilities

    def scope(self, session, run, *, lock=True):
        parent = Repository(session).chat_session(run.user_id, run.session_id, lock=lock)
        context = (run.execution_snapshot or {}).get('context', {})
        execution = self.capabilities.validate_execution(run.user_id, context.get('capability_snapshot'),
                                                        session=session, lock=lock)
        if (execution.catalog.sandbox is None or SANDBOX_TOOL not in execution.allowed_tools
                or context.get('organization_id') != (str(parent.organization_id) if parent.organization_id else None)):
            raise AppError('CAPABILITY_SCOPE_CHANGED', '产物的运行范围无法核验', 409)
        self.capabilities.validate_approval(execution, context.get('tool_approval'),
            user_id=run.user_id, session_id=run.session_id, run_id=run.id, organization_id=parent.organization_id)
        return parent, execution

    @staticmethod
    def metadata(row):
        value = {name: getattr(row, name) for name in METADATA_FIELDS}
        value['created_at'] = aware(row.created_at)
        return value

    @staticmethod
    def require_operation(session, run, claim, key):
        operation = session.get(ChatRunOperation, (run.id, key))
        if (operation is None or operation.state != 'started' or operation.readonly
                or operation.owner_generation != claim.generation):
            raise AppError('RUN_CHECKPOINT_DIVERGED', '产物缺少本次执行持有的工具回执', 409)

    def prepare_execution(self, claim, key):
        with self.store.database.sessions() as session:
            run = self.store.owned(session, claim)
            parent, execution = self.scope(session, run)
            self.require_operation(session, run, claim, key)
            return execution, run.execution_snapshot['context']['tool_approval'], {
                'user_id': run.user_id, 'session_id': run.session_id, 'run_id': run.id,
                'organization_id': parent.organization_id, 'catalog_hash': execution.catalog_hash}

    def save_pending(self, claim, operation_key, files):
        if (not isinstance(files, (list, tuple)) or len(files) > 16
                or any(not isinstance(file, SandboxFile) for file in files)):
            raise AppError('ARTIFACT_INVALID_RESULT', '产物集合无效', 422)
        if any(not isinstance(file.content, bytes) for file in files) or sum(len(file.content) for file in files) > 4194304:
            raise AppError('ARTIFACT_SIZE_LIMIT', '产物集合超过上限', 413)
        prepared = []
        for file in files:
            media_type, preview_kind = classify_artifact(file.name, file.content)
            prepared.append((file, media_type, preview_kind))
        if len({file.name for file in files}) != len(files):
            raise AppError('ARTIFACT_INVALID_RESULT', '产物名称重复', 422)
        with self.store.database.sessions() as session:
            run = self.store.owned(session, claim)
            parent, execution = self.scope(session, run)
            self.require_operation(session, run, claim, operation_key)
            policy = execution.catalog.sandbox
            limits = policy.declaration.limits
            total = sum(len(file.content) for file in files)
            if (len(files) > limits.max_files or total > limits.max_artifact_bytes
                    or any(len(file.content) > limits.max_file_bytes for file in files)):
                raise AppError('ARTIFACT_SIZE_LIMIT', '产物超过本次冻结的资源范围', 413)
            existing = list(session.scalars(select(RunArtifact).where(
                RunArtifact.run_id == run.id, RunArtifact.operation_key == operation_key).order_by(RunArtifact.name)))
            if existing:
                expected = {(file.name, file.sha256, len(file.content)) for file in files}
                if ({(row.name, row.sha256, row.size_bytes) for row in existing} != expected or any(
                        (row.user_id, row.session_id, row.organization_id, row.tool_name, row.tool_version) !=
                        (run.user_id, run.session_id, parent.organization_id, SANDBOX_TOOL, policy.version) for row in existing)):
                    raise AppError('RUN_CHECKPOINT_DIVERGED', '重复回执的产物内容不一致', 409)
                for row in existing:
                    verify_artifact(row)
                return [self.metadata(row) for row in existing]
            count, size = session.execute(select(func.count(), func.coalesce(func.sum(RunArtifact.size_bytes), 0))
                .where(RunArtifact.run_id == run.id)).one()
            if count + len(files) > MAX_RUN_FILES or size + total > MAX_RUN_BYTES:
                raise AppError('ARTIFACT_RUN_LIMIT', '本次运行的产物总量已达上限', 413)
            rows = []
            for file, media_type, preview_kind in sorted(prepared, key=lambda item: item[0].name):
                row = RunArtifact(run_id=run.id, operation_key=operation_key, user_id=run.user_id,
                    session_id=run.session_id, organization_id=parent.organization_id,
                    tool_name=SANDBOX_TOOL, tool_version=policy.version, name=file.name,
                    size_bytes=len(file.content), sha256=file.sha256, media_type=media_type,
                    preview_kind=preview_kind, content=file.content)
                session.add(row)
                rows.append(row)
            session.flush()
            metadata = [self.metadata(row) for row in rows]
            session.commit()
            return metadata

    def authorize(self, session, user_id, run_id):
        run = self.store.get_row(session, user_id, run_id, lock=True)
        parent, execution = self.scope(session, run)
        return run, parent, execution.catalog.sandbox.version

    @staticmethod
    def visible_query(run, parent, version):
        return select(RunArtifact).join(ChatRunOperation,
            (ChatRunOperation.run_id == RunArtifact.run_id) &
            (ChatRunOperation.operation_key == RunArtifact.operation_key)).where(
                RunArtifact.run_id == run.id, RunArtifact.user_id == run.user_id,
                RunArtifact.session_id == run.session_id,
                RunArtifact.organization_id == parent.organization_id,
                RunArtifact.tool_name == SANDBOX_TOOL, RunArtifact.tool_version == version,
                ChatRunOperation.state == 'succeeded')

    def list(self, user_id, run_id):
        with self.store.database.sessions() as session:
            run, parent, version = self.authorize(session, user_id, run_id)
            if run.state not in {'running', 'waiting', 'succeeded'}:
                return []
            rows = session.scalars(self.visible_query(run, parent, version).options(load_only(
                *(getattr(RunArtifact, name) for name in METADATA_FIELDS))).order_by(RunArtifact.created_at, RunArtifact.id))
            return [self.metadata(row) for row in rows]

    def read(self, user_id, run_id, artifact_id, *, preview=False):
        with self.store.database.sessions() as session:
            run, parent, version = self.authorize(session, user_id, run_id)
            if run.state not in {'running', 'waiting', 'succeeded'}:
                raise not_found()
            row = session.scalar(self.visible_query(run, parent, version).where(RunArtifact.id == artifact_id))
            if row is None:
                raise not_found()
            if preview:
                content, media_type = render_artifact_preview(row)
            else:
                verify_artifact(row)
                content, media_type = row.content, row.media_type
            return self.metadata(row), content, media_type
