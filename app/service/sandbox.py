"""A Sandbox call belongs to an existing fenced, non-repeatable Run operation."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import sandbox as queries
from contextlib import contextmanager
from functools import cached_property
import hashlib
import json
from threading import BoundedSemaphore, Lock
from time import monotonic
from uuid import NAMESPACE_URL, uuid5


from app.manager.run_checkpoint import RunCheckpoint
from app.core.serialization import bounded_json
from app.entity.time import aware
from app.core.errors import AppError
from app.entity.bo.sandbox import SandboxExecution, SandboxRequest
from app.manager.sandbox import DockerSandbox
from app.entity.do.models import ChatRun


class SandboxService:
    def __init__(self, settings, capabilities, artifacts):
        self.settings, self.capabilities, self.artifacts = settings, capabilities, artifacts
        self.slots = BoundedSemaphore(2)
        self.reap_lock, self.next_reap = Lock(), 0

    @cached_property
    def namespace(self):
        # Credentials and query options can rotate without orphaning containers.
        url = self.artifacts.store.database.engine.url
        identity = [url.get_backend_name(), url.host, url.port, url.database]
        return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()

    @contextmanager
    def cleanup_guard(self, run_id):
        store = self.artifacts.store
        with transaction_scope(store.database) as session, session.begin():
            # Holding the row lock prevents renewal or takeover between checking and removal.
            run = queries.get_run(run_id, session)
            now = store.now(session)
            active = (run is not None and run.state in {'running', 'cancelling'}
                and run.owner_token is not None and run.lease_expires_at is not None
                and aware(run.lease_expires_at)>now)
            yield not active

    def reap(self):
        if not self.settings.sandbox_enabled or monotonic()<self.next_reap or not self.reap_lock.acquire(blocking=False):
            return 0
        try:
            _, catalog = self.capabilities.snapshot()
            if catalog.sandbox is None:
                return 0
            driver = DockerSandbox(catalog.sandbox.declaration.image,
                require_rootless=self.settings.sandbox_require_rootless, namespace=self.namespace)
            return driver.reap(self.cleanup_guard)
        finally:
            self.next_reap = monotonic()+30
            self.reap_lock.release()

    def readiness(self):
        if not (self.settings.sandbox_enabled and self.settings.tool_approvals_enabled
                and self.settings.capability_center_enabled and self.settings.chat_runs_enabled):
            raise AppError('SANDBOX_CONFIGURATION_REQUIRED', 'Sandbox 执行依赖尚未启用', 503)
        _, catalog = self.capabilities.snapshot()
        if catalog.sandbox is None or not catalog.sandbox.declaration.enabled:
            raise AppError('SANDBOX_POLICY_REQUIRED', '请配置启用的固定镜像策略', 503)
        return DockerSandbox(catalog.sandbox.declaration.image,
            require_rootless=self.settings.sandbox_require_rootless).readiness()

    def execute(self, context, request, checkpoint):
        if (not self.settings.sandbox_enabled or not isinstance(checkpoint, RunCheckpoint)
                or checkpoint.store is not self.artifacts.store or checkpoint.claim.run_id != context.run_id
                or checkpoint.claim.user_id != context.user_id):
            raise AppError('SANDBOX_DURABLE_RUN_REQUIRED', '隔离执行必须绑定持有租约的持久 Run', 409)
        key = checkpoint.operation_key.get()
        if key is None or not key.startswith('tool/'):
            raise AppError('RUN_CHECKPOINT_DIVERGED', '隔离执行缺少当前工具检查点', 409)
        try:
            request = SandboxRequest.model_validate_json(request.model_dump_json(warnings=False))
        except (ValueError, TypeError, AttributeError):
            raise AppError('SANDBOX_INVALID_REQUEST', '隔离执行输入无效', 422) from None
        checkpoint.check()
        execution, approval, identity = self.artifacts.prepare_execution(checkpoint.claim, key)
        if context.session_id != identity['session_id'] or context.organization_id != identity['organization_id']:
            raise AppError('CAPABILITY_SCOPE_CHANGED', '隔离执行与持久会话范围不一致', 409)
        self.capabilities.validate_approval(execution, context.tool_approval,
            **{name: value for name, value in identity.items() if name != 'catalog_hash'})
        policy = execution.catalog.sandbox
        invocation = SandboxExecution(invocation_id=uuid5(NAMESPACE_URL, str(context.run_id)+'/'+key),
            run_id=context.run_id, request=request, image=policy.declaration.image, limits=policy.declaration.limits)
        if not self.slots.acquire(blocking=False):
            raise AppError('SANDBOX_BUSY', '隔离执行并发已达上限', 429)
        try:
            driver = DockerSandbox(invocation.image, require_rootless=self.settings.sandbox_require_rootless,
                namespace=self.namespace)
            result = driver.execute(invocation, approval=approval, identity=identity,
                deadline=context.deadline_monotonic, check=checkpoint.check)
            checkpoint.check()
            metadata = self.artifacts.save_pending(checkpoint.claim, key, result.files) if result.success else []
            return bounded_json({'success': result.success, 'error_code': result.error_code,
                'exit_code': result.exit_code, 'duration_ms': result.duration_ms,
                'stdout': result.stdout, 'stderr': result.stderr, 'artifacts': metadata,
                'language': request.language, 'input_sha256': request.input_hash, 'tool_version': policy.version}, 262144)
        finally:
            self.slots.release()
