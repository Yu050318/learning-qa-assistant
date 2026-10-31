from app.dao.postgres import transactions
from app.core.serialization import bounded_json, checkpoint_json_value
"""Persist public SSE events; reconnects never execute the question again."""
from app.manager.transactions import transaction_scope, as_transaction
from app.dao.queries import runs as queries
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from contextlib import nullcontext
import re
from time import monotonic, sleep
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


from app.dao.postgres.errors import IntegrityError

from app.entity.time import aware
from app.core.errors import AppError, not_found
from app.entity.do.models import ChatEventOutbox, ChatMessage, ChatRun, ChatRunCheckpoint, ChatRunEvent, ChatRunOperation, User
from app.dao.postgres.repository import Repository

logger = logging.getLogger("rag")
ACTIVE_STATES = ("queued", "running", "waiting", "cancelling")
TERMINAL_STATES = ("cancelled", "succeeded", "failed", "interrupted")


@dataclass(frozen=True)
class RunClaim:
    run_id: UUID
    user_id: UUID
    token: UUID
    generation: int


class RunStore:
    @staticmethod
    def public_input_allowed(run):
        from app.entity.bo.capabilities import CapabilityExecution
        try:
            frozen = (run.execution_snapshot or {}).get("context", {}).get("capability_snapshot")
            if frozen is None:
                return False
            execution = CapabilityExecution.model_validate_json(json.dumps(frozen))
            return any(name in execution.catalog.tools for name in execution.allowed_tools)
        except (ValueError, TypeError, AttributeError):
            return False

    def __init__(self, settings, database, *, kind="chat"):
        if kind not in {"chat", "evaluation"}:
            raise ValueError("Unsupported execution queue")
        self.settings, self.database = settings, database
        self.kind = kind

    def now(self, session):
        return transactions.database_now(session)

    def get_row(self, session, user_id, run_id, *, lock=False, authorize=True):
        run = queries.get_run(self.kind, lock, run_id, session, user_id)
        if run is None:
            raise not_found()
        if authorize:
            Repository(session).chat_session(user_id, run.session_id)
        return run

    def guard(self, session, user_id, run_id):
        run = self.get_row(session, user_id, run_id, lock=True)
        if run.state != "running" or aware(run.expires_at) <= self.now(session):
            raise AppError("RUN_INTERRUPTED", "本轮问答已中断，请查看历史后重新提问", 409)
        return run

    def message_state(self, session, run):
        if run.execution_snapshot and run.message_id:
            message = as_transaction(session).load(ChatMessage, run.message_id)
            if message and message.user_id == run.user_id and message.session_id == run.session_id and message.role == "assistant":
                message.run_metadata = {**(message.run_metadata or {}), "run_id": str(run.id), "state": run.state}

    def _event(self, session, run, event, data, *, terminal=False):
        payload = json.loads(json.dumps({**data, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "run_id": str(run.id), "sequence": run.sequence + 1}, default=str))
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        # Reserve room for a terminal error even if model deltas exhaust the budget.
        if size > 2_000_000 or run.sequence >= (8200 if terminal else 8192) or run.event_bytes + size > (8_000_000 if terminal else 6_000_000):
            raise AppError("RUN_EVENT_LIMIT", "本轮流式事件超过保存上限，请缩小问题范围", 413)
        run.sequence += 1
        run.event_bytes += size
        as_transaction(session).save(ChatRunEvent(run_id=run.id, sequence=run.sequence, event=event, payload=payload))
        # Persist the journal row before its dependent notification receipt;
        # both still belong to the caller's single transaction.
        session.flush()
        as_transaction(session).save(ChatEventOutbox(run_id=run.id, sequence=run.sequence))

    def expire(self, session, run):
        if run.state == "waiting" and aware(run.expires_at) <= self.now(session):
            self._event(session, run, "error", {"code": "HITL_EXPIRED", "message": "人工补充已过期", "status": 409}, terminal=True)
            run.state, run.resume_key = "interrupted", None
            self.message_state(session, run)
        elif run.state == "running" and run.generation == 0 and aware(run.expires_at) <= self.now(session):
            self._event(session, run, "error", {"code": "RUN_INTERRUPTED", "message": "服务执行中断或超时，请查看历史后重新提问", "status": 409}, terminal=True)
            run.state = "interrupted"

    def create(self, user_id, session_id, payload, request_id, client_key, *, queued=False, prepare=None):
        if queued and prepare is None:
            raise ValueError("Queued runs require an atomic preparation callback")
        inputs = asdict(payload)
        if inputs.get("model_profile_id") is None:
            inputs.pop("model_profile_id", None)
        if inputs.get("skill_name") is None:
            inputs.pop("skill_name", None)
        if not inputs.get("approved_tools") and inputs.get("approval_catalog_revision") is None:
            inputs.pop("approved_tools", None)
            inputs.pop("approval_catalog_revision", None)
        elif inputs.get("approved_tools"):
            inputs["approved_tools"] = sorted(inputs["approved_tools"])
        if not inputs.get("knowledge_base_ids"):
            inputs.pop("knowledge_base_ids", None)  # Preserve phase-nine keys across this additive request field.
        fingerprint = hashlib.sha256(json.dumps({"session_id": str(session_id), "input": inputs}, sort_keys=True, default=str).encode()).hexdigest()
        try:
            with transaction_scope(self.database) as session:
                if queued:
                    # Per-user admission serializes across API instances; exact
                    # retries still return the original Run even at capacity.
                    user = queries.lock_user(session, user_id)
                    if user is None or not user.is_active:
                        raise AppError("FORBIDDEN", "账号已停用", 403)
                existing = queries.find_idempotent_run(client_key, session, user_id)
                if existing:
                    if existing.fingerprint != fingerprint or existing.execution_kind != self.kind:
                        raise AppError("IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同的问题或会话", 409)
                    self.expire(session, existing)
                    session.commit()
                    return existing, False
                # An exact retry needs only the existing Run lock. ChatService may
                # briefly hold the session row while preparing/saving the answer.
                Repository(session).chat_session(user_id, session_id, lock=True, kind=self.kind)
                active = queries.active_session_run(ACTIVE_STATES, session, session_id)
                if active:
                    self.expire(session, active)
                    if active.state in ACTIVE_STATES:
                        raise AppError("RESOURCE_BUSY", "会话已有正在执行的问答", 409)
                    session.flush()
                if queued:
                    count = queries.count_active_runs(ACTIVE_STATES, self.kind, session, user_id)
                    if count >= self.settings.chat_run_queue_limit:
                        raise AppError("RUN_CAPACITY_EXCEEDED", "待处理问答已达上限，请稍后用原请求标识重试", 429)
                run = ChatRun(id=uuid4(), user_id=user_id, session_id=session_id, client_key=client_key,
                              fingerprint=fingerprint, request_id=request_id, state="queued" if queued else "running", execution_kind=self.kind, sequence=0, event_bytes=0,
                              payload=bounded_json(inputs, 100_000), remaining_seconds=self.settings.agent_timeout if queued else None,
                              expires_at=self.now(session) + timedelta(seconds=self.settings.agent_timeout + 30))
                as_transaction(session).save(run);session.flush()
                if prepare is not None:
                    prepare(session, run, payload)
                    run.execution_snapshot = bounded_json(run.execution_snapshot, 4_000_000)
                self._event(session, run, "run_started", {"request_id": request_id, "search_mode": payload.search_mode})
                approval = (run.execution_snapshot or {}).get("context", {}).get("tool_approval")
                if approval:
                    self._event(session, run, "tool_approval_granted", {"tool_names": approval["tool_names"], "catalog_hash": approval["catalog_hash"]})
                session.commit()
                return run, True
        except IntegrityError:
            # Race on client key across different sessions: never start a second execution.
            raise AppError("IDEMPOTENCY_CONFLICT", "请求标识已使用，请用原标识查询结果", 409) from None

    def append(self, user_id, run_id, event, value, *, claim=None):
        if event == "answer_delta":
            data = {"delta": value}
        elif event in {"agent_started", "agent_finished", "agent_failed"}:
            data = value
        elif event == "answer_start":
            data = {}
        else:
            raise AppError("RUN_EVENT_INVALID", "流式事件类型不受支持", 500)
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim) if claim else self.guard(session, user_id, run_id)
            if run.id != run_id or run.user_id != user_id:
                raise AppError("RUN_LEASE_LOST", "运行租约不匹配", 409)
            self._event(session, run, event, data)
            session.commit()

    def complete(self, session, user_id, run_id, message, *, claim=None):
        """Assistant message and terminal events commit together in ChatService."""
        run = self.owned(session, claim) if claim else self.guard(session, user_id, run_id)
        if (run.id != run_id or run.user_id != user_id or message.user_id != user_id
                or message.session_id != run.session_id or message.role != "assistant"
                or (claim and run.message_id != message.id)):
            raise AppError("RUN_CHECKPOINT_DIVERGED", "运行消息与检查点不匹配", 409)
        metadata = message.run_metadata or {}
        metrics = metadata.get("metrics", {})
        if metrics.get("context"):
            self._event(session, run, "context_usage", {"context": metrics["context"]}, terminal=True)
        self._event(session, run, "usage", {"token_usage": message.token_usage, "cache": metrics.get("cache")}, terminal=True)
        self._event(session, run, "done", {"message_id": message.id, "answer": message.content,
                    "model_provider": message.model_provider, "model_name": message.model_name,
                    "search_mode": metadata.get("search_mode", "auto"), "citations": message.citations,
                    "token_usage": message.token_usage, "run_metadata": metadata}, terminal=True)
        run.state, run.message_id = "succeeded", message.id
        run.owner_token = run.lease_expires_at = run.active_since = None

    def fail(self, user_id, run_id, error):
        with transaction_scope(self.database) as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            if run.state != "running":
                return
            self.expire(session, run)
            if run.state == "running":
                self._event(session, run, "error", {"code": error.code if isinstance(error, AppError) else "INTERNAL_ERROR",
                            "message": error.message if isinstance(error, AppError) else "服务内部错误，请根据 request_id 检查日志",
                            "status": error.status_code if isinstance(error, AppError) else 500,
                            "request_id": run.request_id}, terminal=True)
                run.state = "failed"
            session.commit()

    def read(self, user_id, run_id, after=0):
        with transaction_scope(self.database) as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            self.expire(session, run)
            waiting_event = as_transaction(session).load(ChatRunEvent, (run.id, run.sequence)) if run.state == "waiting" else None
            run.waiting = dict(waiting_event.payload) if waiting_event and waiting_event.event == "run_waiting" else None
            if after > run.sequence:
                raise AppError("INVALID_EVENT_CURSOR", "事件游标超过已保存的序号", 422)
            session.flush()
            events = queries.list_events_after(after, run_id, session)
            session.commit()
            return run, events

    def owned(self, session, claim, *, allow_cancelling=False, check_deadline=True):
        # Workers must still settle a fenced failure after workspace revocation.
        # Public read/control methods authorize the session via get_row's default.
        run = self.get_row(session, claim.user_id, claim.run_id, lock=True, authorize=False)
        if (run.owner_token != claim.token or run.generation != claim.generation
                or run.lease_expires_at is None or aware(run.lease_expires_at) <= self.now(session)
                or run.state not in {"running", "cancelling"}):
            raise AppError("RUN_LEASE_LOST", "运行租约已失效", 409)
        if run.state == "cancelling" and not allow_cancelling:
            raise AppError("RUN_CANCEL_REQUESTED", "用户已请求取消运行", 409)
        if check_deadline and aware(run.expires_at) <= self.now(session):
            raise AppError("AGENT_TIMEOUT", "运行预算已耗尽", 504)
        return run

    def settle_cancellations(self, session):
        now = self.now(session)
        cancelled = queries.lock_expired_cancellations(now, self.kind, session)
        for abandoned in cancelled:
            abandoned.state = "cancelled"
            self.message_state(session, abandoned)
            abandoned.owner_token = abandoned.lease_expires_at = abandoned.active_since = None
            self._event(session, abandoned, "run_cancelled", {}, terminal=True)

    def claim_next(self, run_id=None):
        with transaction_scope(self.database) as session:
            now = self.now(session)
            expired = queries.lock_expired_waiting_runs(now, self.kind, session)
            for waiting in expired:
                self.expire(session, waiting)
            self.settle_cancellations(session)
            run = queries.lock_next_run(now, run_id, self.kind, session)
            if run is None:
                session.commit()
                return None
            remaining = run.remaining_seconds if run.remaining_seconds is not None else self.settings.agent_timeout
            if run.active_since is not None:
                end = min(now, aware(run.lease_expires_at))
                remaining -= max(0, (end - aware(run.active_since)).total_seconds())
            if remaining <= 0:
                run.state = "failed"
                self.message_state(session, run)
                self._event(session, run, "error", {"code": "AGENT_TIMEOUT", "message": "运行预算已耗尽", "status": 504}, terminal=True)
                session.commit()
                return None
            run.state = "running"
            run.owner_token = uuid4()
            run.generation += 1
            run.remaining_seconds, run.active_since = remaining, now
            run.expires_at = now + timedelta(seconds=remaining)
            run.lease_expires_at = now + timedelta(seconds=self.settings.chat_run_lease_seconds)
            self.message_state(session, run)
            claim = RunClaim(run.id, run.user_id, run.owner_token, run.generation)
            self._event(session, run, "run_claimed", {"generation": run.generation})
            session.commit()
            return claim

    def release_busy(self, claim):
        """A surviving session lock delays recovery without resetting its budget."""
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim)
            now = self.now(session)
            run.remaining_seconds -= max(0, (now - aware(run.active_since)).total_seconds())
            run.state, run.owner_token, run.active_since = "queued", None, None
            self.message_state(session, run)
            run.lease_expires_at = now + timedelta(seconds=1)
            session.commit()

    def heartbeat(self, claim):
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim)
            run.lease_expires_at = self.now(session) + timedelta(seconds=self.settings.chat_run_lease_seconds)
            session.commit()

    def check_owned(self, claim):
        with transaction_scope(self.database) as session:
            self.owned(session, claim)

    def save_checkpoint(self, session, run, payload):
        if not isinstance(payload, dict):
            raise ValueError("A checkpoint must be a JSON object")
        payload = bounded_json(payload, 4_000_000)
        row = as_transaction(session).load(ChatRunCheckpoint, run.id)
        if row is None:
            row = ChatRunCheckpoint(run_id=run.id, revision=1, payload=payload)
            as_transaction(session).save(row)
        else:
            row.payload, row.revision = payload, row.revision + 1
        return row

    def checkpoint(self, claim, payload):
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim)
            revision = self.save_checkpoint(session, run, payload).revision
            session.commit()
            return revision

    def load_checkpoint(self, claim):
        with transaction_scope(self.database) as session:
            self.owned(session, claim)
            row = as_transaction(session).load(ChatRunCheckpoint, claim.run_id)
            return (row.revision, dict(row.payload)) if row else (0, {})

    def request_cancel(self, user_id, run_id):
        with transaction_scope(self.database) as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            if run.state not in TERMINAL_STATES and run.state != "cancelling":
                queued = run.state in {"queued", "waiting"} or run.owner_token is None or (
                    run.lease_expires_at is not None and aware(run.lease_expires_at) <= self.now(session))
                run.state, run.resume_key = ("cancelled" if queued else "cancelling"), None
                self.message_state(session, run)
                self._event(session, run, "run_cancelled" if queued else "cancel_requested", {}, terminal=queued)
            session.commit()
            return run

    def finish_failure(self, claim, error):
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim, allow_cancelling=True, check_deadline=False)
            if run.state == "cancelling" and not (isinstance(error, AppError) and error.code == "SANDBOX_CLEANUP_FAILED"):
                run.state = "cancelled"
                self._event(session, run, "run_cancelled", {}, terminal=True)
            else:
                run.state = "failed"
                self._event(session, run, "error", {"code": error.code if isinstance(error, AppError) else "RUN_EXECUTION_FAILED",
                    "message": error.message if isinstance(error, AppError) else "运行失败，请根据 request_id 检查日志",
                    "status": error.status_code if isinstance(error, AppError) else 500}, terminal=True)
            run.owner_token = run.lease_expires_at = run.active_since = None
            self.message_state(session, run)
            session.commit()

    def pause(self, claim, payload, question, *, session=None):
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 4000:
            raise AppError("INVALID_CLARIFICATION", "澄清问题无效", 422)
        own_transaction = session is None
        with (transaction_scope(self.database) if own_transaction else nullcontext(session)) as session:
            run = self.owned(session, claim)
            row = self.save_checkpoint(session, run, payload)
            now = self.now(session)
            remaining = run.remaining_seconds - max(0, (now - aware(run.active_since)).total_seconds())
            if remaining <= 0:
                raise AppError("AGENT_TIMEOUT", "运行预算已耗尽", 504)
            run.state, run.remaining_seconds, run.resume_key = "waiting", remaining, uuid4()
            run.expires_at = now + timedelta(seconds=self.settings.chat_run_wait_seconds)
            run.owner_token = run.lease_expires_at = run.active_since = None
            self.message_state(session, run)
            self._event(session, run, "run_waiting", {"question": question.strip(), "checkpoint_revision": row.revision,
                "resume_token": str(run.resume_key), "expires_at": run.expires_at,
                **({"public_input_allowed": True} if self.public_input_allowed(run) else {})})
            if own_transaction:
                session.commit()
            return run

    def resume(self, user_id, run_id, revision, resume_key, supplement, *, public_query=None):
        if not isinstance(supplement, str) or not 1 <= len(supplement.strip()) <= 4000:
            raise AppError("INVALID_RESUME_INPUT", "补充信息须为 1 至 4000 个字符", 422)
        supplement = supplement.strip()
        if public_query is not None:
            if not isinstance(public_query, str) or not 1 <= len(public_query.strip()) <= 400:
                raise AppError("INVALID_PUBLIC_INPUT", "公开查询须为 1 至 400 个字符", 422)
            from app.service.chat import prepare_public_query
            from app.entity.bo.contracts import AgentChatInput
            public_query, _, _ = prepare_public_query(AgentChatInput(public_query.strip(), "web"), False)
        input_hash = hashlib.sha256(supplement.encode()).hexdigest()
        if public_query is not None:
            input_hash = hashlib.sha256(json.dumps({"supplement": supplement, "public_query": public_query}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with transaction_scope(self.database) as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            row = as_transaction(session).load(ChatRunCheckpoint, run.id)
            receipt = (row.payload if row else {}).get("resume_receipt", {})
            if receipt.get("key") == str(resume_key):
                if receipt.get("input_hash") != input_hash or receipt.get("revision") != revision:
                    raise AppError("RESUME_CONFLICT", "同一恢复标识不能用于不同的补充内容", 409)
                return run
            if run.state != "waiting" or not row or row.revision != revision or run.resume_key != resume_key:
                raise AppError("RESUME_CONFLICT", "运行状态或检查点已变化，请刷新后重试", 409)
            context = (run.execution_snapshot or {}).get("context", {})
            if context.get("tool_approval") is not None:
                from app.service.capabilities import CapabilityService
                service = CapabilityService(self.settings, self.database)
                execution = service.validate_execution(user_id, context.get("capability_snapshot"), session=session)
                parent = Repository(session).chat_session(user_id, run.session_id)
                service.validate_approval(execution, context["tool_approval"], user_id=user_id, session_id=run.session_id,
                    run_id=run.id, organization_id=parent.organization_id)
            if public_query is not None:
                if not self.public_input_allowed(run):
                    raise AppError("PUBLIC_INPUT_NOT_ALLOWED", "当前运行未授权公共 HTTP 工具", 409)
                from app.service.capabilities import CapabilityService
                CapabilityService(self.settings, self.database).validate_execution(user_id,
                    run.execution_snapshot["context"]["capability_snapshot"], session=session)
            if aware(run.expires_at) <= self.now(session):
                run.state, run.resume_key = "interrupted", None
                self.message_state(session, run)
                self._event(session, run, "error", {"code": "HITL_EXPIRED", "message": "人工补充已过期", "status": 409}, terminal=True)
                session.commit()
                raise AppError("HITL_EXPIRED", "人工补充已过期", 409)
            payload = dict(row.payload)
            if public_query is not None or "public_inputs" in payload:
                previous = payload.get("public_inputs", [None] * len(payload.get("human_inputs", [])))
                if not isinstance(previous, list) or len(previous) != len(payload.get("human_inputs", [])):
                    raise AppError("RUN_CHECKPOINT_DIVERGED", "公开补充输入与运行轮次不一致", 409)
                payload["public_inputs"] = [*previous, public_query]
            payload["human_inputs"] = [*payload.get("human_inputs", []), supplement]
            payload["resume_receipt"] = {"key": str(resume_key), "input_hash": input_hash, "revision": revision}
            row.payload, row.revision = bounded_json(payload, 4_000_000), row.revision + 1
            run.state, run.resume_key = "queued", None
            self.message_state(session, run)
            self._event(session, run, "run_resumed", {"checkpoint_revision": row.revision})
            session.commit()
            return run

    def begin_operation(self, claim, key, inputs, *, readonly):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", key):
            raise ValueError("Invalid operation key")
        inputs = bounded_json(inputs, 2_000_000)
        input_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim)
            row = as_transaction(session).load(ChatRunOperation, (claim.run_id, key))
            if row:
                if row.input_hash != input_hash or row.readonly != readonly:
                    raise AppError("RUN_CHECKPOINT_DIVERGED", "检查点调用与已保存输入不一致", 409)
                if row.state == "succeeded":
                    return True, row.result
                if row.owner_generation == claim.generation and row.state == "started":
                    raise AppError("OPERATION_ALREADY_RUNNING", "同一检查点调用正在执行", 409)
                if not readonly:
                    row.state = "uncertain"
                    if key.startswith('tool/'):
                        self._event(session, run, 'tool_failed', {'operation_key':key,
                            'status':'uncertain', 'error_code':'TOOL_OUTCOME_UNCERTAIN'})
                    session.commit()
                    raise AppError("TOOL_OUTCOME_UNCERTAIN", "工具结果尚未确认，需人工核查后处理", 409)
                if row.attempts >= 3:
                    raise AppError("RUN_RECOVERY_LIMIT", "检查点调用恢复次数超过上限", 409)
                row.state, row.owner_generation, row.attempts = "started", claim.generation, row.attempts + 1
            else:
                count = queries.operation_count(claim, session)
                if count >= 128:
                    raise AppError("RUN_CHECKPOINT_LIMIT", "运行检查点调用超过上限", 413)
                as_transaction(session).save(ChatRunOperation(run_id=claim.run_id, operation_key=key, input_hash=input_hash,
                    state="started", readonly=readonly, owner_generation=claim.generation, attempts=1))
            if key.startswith('tool/'):
                name = inputs.get('name') if isinstance(inputs, dict) else None
                self._event(session, run, 'tool_started', {'operation_key':key,
                    'name':name if isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',name) else None,
                    'generation':claim.generation,'readonly':readonly})
            session.commit()
            return False, None

    def complete_operation(self, claim, key, result, *, checkpoint_payload=None):
        result = bounded_json(result, 2_000_000)
        with transaction_scope(self.database) as session:
            run = self.owned(session, claim)
            row = as_transaction(session).load(ChatRunOperation, (claim.run_id, key))
            if not row or row.state != "started" or row.owner_generation != claim.generation:
                raise AppError("RUN_CHECKPOINT_DIVERGED", "检查点调用状态已变化", 409)
            row.state, row.result = "succeeded", result
            if key.startswith('tool/'):
                receipt = result if isinstance(result,dict) else {}
                value = receipt.get('value') if isinstance(receipt.get('value'), dict) else {}
                error = value.get('error') if isinstance(value.get('error'), dict) else {}
                output = value.get('value') if isinstance(value.get('value'),dict) else {}
                code = receipt.get('code') if receipt.get('ok') is False else error.get('code') or output.get('error_code')
                failed = receipt.get('ok') is False or bool(error) or output.get('status')=='failed' or output.get('success') is False
                self._event(session, run, 'tool_failed' if failed else 'tool_finished', {
                    'operation_key':key, 'status':'failed' if failed else 'completed',
                    'error_code':code if isinstance(code,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',code) else None,
                    'elapsed_ms':round(max(0,(datetime.now(timezone.utc)-aware(row.updated_at)).total_seconds()*1000),2)})
            if self.settings.sandbox_enabled and not row.readonly:
                from app.entity.do.models import RunArtifact
                artifacts = queries.operation_artifacts(key, run, session)
                if artifacts:
                    self._event(session, run, "artifacts_ready", {"items": [{"id": str(item.id), "name": item.name,
                        "size_bytes": item.size_bytes, "sha256": item.sha256, "media_type": item.media_type,
                        "preview_kind": item.preview_kind, "tool_name": item.tool_name,
                        "tool_version": item.tool_version} for item in artifacts]})
            if checkpoint_payload is not None:
                self.save_checkpoint(session, run, checkpoint_payload)
            session.commit()

    def dispatch_outbox(self, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("Outbox dispatch limit must be 1..100")
        with transaction_scope(self.database) as session:
            rows = queries.lock_pending_events(limit, session)
            for row in rows:
                if queries.supports_notifications(session):
                    queries.notify_event(row, session)
                row.dispatched_at = self.now(session)
            session.commit()
            return len(rows)


class RunService:
    def list_for_session(self, user_id, session_id):
        from app.dao.postgres.chat_queries import recent_run_ids
        with transaction_scope(self.store.database) as transaction:
            Repository(transaction).chat_session(user_id, session_id, kind="chat")
            ids = recent_run_ids(transaction, user_id, session_id)
        return [self.store.read(user_id, run_id)[0] for run_id in ids]

    def __init__(self, settings, database, chat):
        self.store = RunStore(settings, database)
        from app.manager.run_events import RunNotifications
        self.notifications = RunNotifications(self.store)
        self.chat = chat

    def close(self):
        self.notifications.close()

    def start(self, user_id, session_id, payload, request_id, client_key):
        return self.store.create(user_id, session_id, payload, request_id, client_key,
                                 queued=True, prepare=self.chat.prepare_run)[0]

    def wait(self, user_id, run_id):
        deadline = monotonic() + self.store.settings.agent_timeout + 30
        while True:
            run, events = self.store.read(user_id, run_id)
            if run.state in TERMINAL_STATES or run.state == "waiting" or monotonic() >= deadline:
                if events and events[-1].sequence < run.sequence and run.state in TERMINAL_STATES:
                    run, events = self.store.read(user_id, run_id, run.sequence - 1)
                return run, events
            sleep(.2)
