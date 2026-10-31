"""Fenced, Run-owned artifact storage. Pending tool results are never public."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import artifacts as queries


from app.entity.time import aware

from app.core.errors import AppError, not_found
from app.entity.bo.artifacts import classify_artifact, render_artifact_preview, verify_artifact
from app.entity.bo.capabilities import SANDBOX_TOOL
from app.entity.bo.sandbox import SandboxFile
from app.entity.do.models import ChatRunOperation, RunArtifact
from app.dao.postgres.repository import Repository

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
        operation = as_transaction(session).load(ChatRunOperation, (run.id, key))
        if (operation is None or operation.state != 'started' or operation.readonly
                or operation.owner_generation != claim.generation):
            raise AppError('RUN_CHECKPOINT_DIVERGED', '产物缺少本次执行持有的工具回执', 409)

    def prepare_execution(self, claim, key):
        with transaction_scope(self.store.database) as session:
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
        with transaction_scope(self.store.database) as session:
            run = self.store.owned(session, claim)
            parent, execution = self.scope(session, run)
            self.require_operation(session, run, claim, operation_key)
            policy = execution.catalog.sandbox
            limits = policy.declaration.limits
            total = sum(len(file.content) for file in files)
            if (len(files) > limits.max_files or total > limits.max_artifact_bytes
                    or any(len(file.content) > limits.max_file_bytes for file in files)):
                raise AppError('ARTIFACT_SIZE_LIMIT', '产物超过本次冻结的资源范围', 413)
            existing = queries.operation_artifacts(operation_key, run, session)
            if existing:
                expected = {(file.name, file.sha256, len(file.content)) for file in files}
                if ({(row.name, row.sha256, row.size_bytes) for row in existing} != expected or any(
                        (row.user_id, row.session_id, row.organization_id, row.tool_name, row.tool_version) !=
                        (run.user_id, run.session_id, parent.organization_id, SANDBOX_TOOL, policy.version) for row in existing)):
                    raise AppError('RUN_CHECKPOINT_DIVERGED', '重复回执的产物内容不一致', 409)
                for row in existing:
                    verify_artifact(row)
                return [self.metadata(row) for row in existing]
            count, size = queries.run_totals(run, session)
            if count + len(files) > MAX_RUN_FILES or size + total > MAX_RUN_BYTES:
                raise AppError('ARTIFACT_RUN_LIMIT', '本次运行的产物总量已达上限', 413)
            rows = []
            for file, media_type, preview_kind in sorted(prepared, key=lambda item: item[0].name):
                row = RunArtifact(run_id=run.id, operation_key=operation_key, user_id=run.user_id,
                    session_id=run.session_id, organization_id=parent.organization_id,
                    tool_name=SANDBOX_TOOL, tool_version=policy.version, name=file.name,
                    size_bytes=len(file.content), sha256=file.sha256, media_type=media_type,
                    preview_kind=preview_kind, content=file.content)
                as_transaction(session).save(row)
                rows.append(row)
            session.flush()
            metadata = [self.metadata(row) for row in rows]
            session.commit()
            return metadata

    def authorize(self, session, user_id, run_id):
        run = self.store.get_row(session, user_id, run_id, lock=True)
        parent, execution = self.scope(session, run)
        return run, parent, execution.catalog.sandbox.version

    def list(self, user_id, run_id):
        with transaction_scope(self.store.database) as session:
            run, parent, version = self.authorize(session, user_id, run_id)
            if run.state not in {'running', 'waiting', 'succeeded'}:
                return []
            rows = queries.list_visible_metadata(METADATA_FIELDS, parent, run, session, version)
            return [self.metadata(row) for row in rows]

    def read(self, user_id, run_id, artifact_id, *, preview=False):
        with transaction_scope(self.store.database) as session:
            run, parent, version = self.authorize(session, user_id, run_id)
            if run.state not in {'running', 'waiting', 'succeeded'}:
                raise not_found()
            row = queries.get_visible_artifact(artifact_id, parent, run, session, version)
            if row is None:
                raise not_found()
            if preview:
                content, media_type = render_artifact_preview(row)
            else:
                verify_artifact(row)
                content, media_type = row.content, row.media_type
            return self.metadata(row), content, media_type
