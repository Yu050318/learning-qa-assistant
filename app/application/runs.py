"""Persist public SSE events; reconnects never execute the question again."""
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from contextlib import nullcontext
import re
from time import monotonic, sleep
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.application.ingestion_jobs import aware
from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import ChatEventOutbox, ChatMessage, ChatRun, ChatRunCheckpoint, ChatRunEvent, ChatRunOperation, User
from app.infrastructure.persistence.repository import Repository

logger = logging.getLogger("rag")
ACTIVE_STATES = ("queued", "running", "waiting", "cancelling")
TERMINAL_STATES = ("cancelled", "succeeded", "failed", "interrupted")


@dataclass(frozen=True)
class RunClaim:
    run_id: UUID
    user_id: UUID
    token: UUID
    generation: int


def checkpoint_json_value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Checkpoint values must be JSON data, UUIDs or timestamps")


def bounded_json(value, max_bytes):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=checkpoint_json_value)
    if len(encoded.encode()) > max_bytes:
        raise AppError("RUN_CHECKPOINT_LIMIT", "运行检查点超过保存上限", 413)
    return json.loads(encoded)


class RunStore:
    def __init__(self, settings, database, *, kind="chat"):
        if kind not in {"chat", "evaluation"}:
            raise ValueError("Unsupported execution queue")
        self.settings, self.database = settings, database
        self.kind = kind

    def now(self, session):
        clock = func.clock_timestamp() if session.get_bind().dialect.name == "postgresql" else func.current_timestamp()
        return aware(session.scalar(select(clock)))

    def get_row(self, session, user_id, run_id, *, lock=False, authorize=True):
        query = select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id, ChatRun.execution_kind == self.kind)
        run = session.scalar(query.with_for_update() if lock else query)
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
            message = session.get(ChatMessage, run.message_id)
            if message and message.user_id == run.user_id and message.session_id == run.session_id and message.role == "assistant":
                message.run_metadata = {**(message.run_metadata or {}), "run_id": str(run.id), "state": run.state}

    def _event(self, session, run, event, data, *, terminal=False):
        payload = json.loads(json.dumps({**data, "run_id": str(run.id), "sequence": run.sequence + 1}, default=str))
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        # Reserve room for a terminal error even if model deltas exhaust the budget.
        if size > 2_000_000 or run.sequence >= (8200 if terminal else 8192) or run.event_bytes + size > (8_000_000 if terminal else 6_000_000):
            raise AppError("RUN_EVENT_LIMIT", "本轮流式事件超过保存上限，请缩小问题范围", 413)
        run.sequence += 1
        run.event_bytes += size
        session.add(ChatRunEvent(run_id=run.id, sequence=run.sequence, event=event, payload=payload))
        # Persist the journal row before its dependent notification receipt;
        # both still belong to the caller's single transaction.
        session.flush()
        session.add(ChatEventOutbox(run_id=run.id, sequence=run.sequence))

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
        if not inputs.get("knowledge_base_ids"):
            inputs.pop("knowledge_base_ids", None)  # Preserve phase-nine keys across this additive request field.
        fingerprint = hashlib.sha256(json.dumps({"session_id": str(session_id), "input": inputs}, sort_keys=True, default=str).encode()).hexdigest()
        try:
            with self.database.sessions() as session:
                if queued:
                    # Per-user admission serializes across API instances; exact
                    # retries still return the original Run even at capacity.
                    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
                    if user is None or not user.is_active:
                        raise AppError("FORBIDDEN", "账号已停用", 403)
                existing = session.scalar(select(ChatRun).where(ChatRun.user_id == user_id, ChatRun.client_key == client_key).with_for_update())
                if existing:
                    if existing.fingerprint != fingerprint or existing.execution_kind != self.kind:
                        raise AppError("IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同的问题或会话", 409)
                    self.expire(session, existing)
                    session.commit()
                    return existing, False
                # An exact retry needs only the existing Run lock. ChatService may
                # briefly hold the session row while preparing/saving the answer.
                Repository(session).chat_session(user_id, session_id, lock=True, kind=self.kind)
                active = session.scalar(select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.state.in_(ACTIVE_STATES)).with_for_update())
                if active:
                    self.expire(session, active)
                    if active.state in ACTIVE_STATES:
                        raise AppError("RESOURCE_BUSY", "会话已有正在执行的问答", 409)
                    session.flush()
                if queued:
                    count = session.scalar(select(func.count()).select_from(ChatRun).where(
                        ChatRun.user_id == user_id, ChatRun.execution_kind == self.kind, ChatRun.state.in_(ACTIVE_STATES)))
                    if count >= self.settings.chat_run_queue_limit:
                        raise AppError("RUN_CAPACITY_EXCEEDED", "待处理问答已达上限，请稍后用原请求标识重试", 429)
                run = ChatRun(id=uuid4(), user_id=user_id, session_id=session_id, client_key=client_key,
                              fingerprint=fingerprint, request_id=request_id, state="queued" if queued else "running", execution_kind=self.kind, sequence=0, event_bytes=0,
                              payload=bounded_json(inputs, 100_000), remaining_seconds=self.settings.agent_timeout if queued else None,
                              expires_at=self.now(session) + timedelta(seconds=self.settings.agent_timeout + 30))
                session.add(run);session.flush()
                if prepare is not None:
                    prepare(session, run, payload)
                    run.execution_snapshot = bounded_json(run.execution_snapshot, 4_000_000)
                self._event(session, run, "run_started", {"request_id": request_id, "search_mode": payload.search_mode})
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
        with self.database.sessions() as session:
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
        with self.database.sessions() as session:
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
        with self.database.sessions() as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            self.expire(session, run)
            waiting_event = session.get(ChatRunEvent, (run.id, run.sequence)) if run.state == "waiting" else None
            run.waiting = dict(waiting_event.payload) if waiting_event and waiting_event.event == "run_waiting" else None
            if after > run.sequence:
                raise AppError("INVALID_EVENT_CURSOR", "事件游标超过已保存的序号", 422)
            session.flush()
            events = session.scalars(select(ChatRunEvent).where(ChatRunEvent.run_id == run_id, ChatRunEvent.sequence > after)
                                     .order_by(ChatRunEvent.sequence).limit(100)).all()
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
        cancelled = session.scalars(select(ChatRun).where(ChatRun.execution_kind == self.kind, ChatRun.state == "cancelling", or_(
            ChatRun.lease_expires_at <= now, ChatRun.owner_token.is_(None)))
            .limit(10).with_for_update(skip_locked=True)).all()
        for abandoned in cancelled:
            abandoned.state = "cancelled"
            self.message_state(session, abandoned)
            abandoned.owner_token = abandoned.lease_expires_at = abandoned.active_since = None
            self._event(session, abandoned, "run_cancelled", {}, terminal=True)

    def claim_next(self, run_id=None):
        with self.database.sessions() as session:
            now = self.now(session)
            expired = session.scalars(select(ChatRun).where(ChatRun.execution_kind == self.kind, ChatRun.state == "waiting", ChatRun.expires_at <= now)
                .limit(10).with_for_update(skip_locked=True)).all()
            for waiting in expired:
                self.expire(session, waiting)
            self.settle_cancellations(session)
            run = session.scalar(select(ChatRun).where(ChatRun.execution_kind == self.kind,
                ChatRun.id == run_id if run_id is not None else True, or_(and_(ChatRun.state == "queued", or_(
                ChatRun.lease_expires_at.is_(None), ChatRun.lease_expires_at <= now)), and_(
                ChatRun.state == "running", ChatRun.generation > 0, ChatRun.lease_expires_at <= now)))
                .order_by(ChatRun.created_at, ChatRun.id).limit(1).with_for_update(skip_locked=True))
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
        with self.database.sessions() as session:
            run = self.owned(session, claim)
            now = self.now(session)
            run.remaining_seconds -= max(0, (now - aware(run.active_since)).total_seconds())
            run.state, run.owner_token, run.active_since = "queued", None, None
            self.message_state(session, run)
            run.lease_expires_at = now + timedelta(seconds=1)
            session.commit()

    def heartbeat(self, claim):
        with self.database.sessions() as session:
            run = self.owned(session, claim)
            run.lease_expires_at = self.now(session) + timedelta(seconds=self.settings.chat_run_lease_seconds)
            session.commit()

    def check_owned(self, claim):
        with self.database.sessions() as session:
            self.owned(session, claim)

    def save_checkpoint(self, session, run, payload):
        if not isinstance(payload, dict):
            raise ValueError("A checkpoint must be a JSON object")
        payload = bounded_json(payload, 4_000_000)
        row = session.get(ChatRunCheckpoint, run.id)
        if row is None:
            row = ChatRunCheckpoint(run_id=run.id, revision=1, payload=payload)
            session.add(row)
        else:
            row.payload, row.revision = payload, row.revision + 1
        return row

    def checkpoint(self, claim, payload):
        with self.database.sessions() as session:
            run = self.owned(session, claim)
            revision = self.save_checkpoint(session, run, payload).revision
            session.commit()
            return revision

    def load_checkpoint(self, claim):
        with self.database.sessions() as session:
            self.owned(session, claim)
            row = session.get(ChatRunCheckpoint, claim.run_id)
            return (row.revision, dict(row.payload)) if row else (0, {})

    def request_cancel(self, user_id, run_id):
        with self.database.sessions() as session:
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
        with self.database.sessions() as session:
            run = self.owned(session, claim, allow_cancelling=True, check_deadline=False)
            if run.state == "cancelling":
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
        with (self.database.sessions() if own_transaction else nullcontext(session)) as session:
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
                "resume_token": str(run.resume_key), "expires_at": run.expires_at})
            if own_transaction:
                session.commit()
            return run

    def resume(self, user_id, run_id, revision, resume_key, supplement):
        if not isinstance(supplement, str) or not 1 <= len(supplement.strip()) <= 4000:
            raise AppError("INVALID_RESUME_INPUT", "补充信息须为 1 至 4000 个字符", 422)
        supplement = supplement.strip()
        input_hash = hashlib.sha256(supplement.encode()).hexdigest()
        with self.database.sessions() as session:
            run = self.get_row(session, user_id, run_id, lock=True)
            row = session.get(ChatRunCheckpoint, run.id)
            receipt = (row.payload if row else {}).get("resume_receipt", {})
            if receipt.get("key") == str(resume_key):
                if receipt.get("input_hash") != input_hash or receipt.get("revision") != revision:
                    raise AppError("RESUME_CONFLICT", "同一恢复标识不能用于不同的补充内容", 409)
                return run
            if run.state != "waiting" or not row or row.revision != revision or run.resume_key != resume_key:
                raise AppError("RESUME_CONFLICT", "运行状态或检查点已变化，请刷新后重试", 409)
            if aware(run.expires_at) <= self.now(session):
                run.state, run.resume_key = "interrupted", None
                self.message_state(session, run)
                self._event(session, run, "error", {"code": "HITL_EXPIRED", "message": "人工补充已过期", "status": 409}, terminal=True)
                session.commit()
                raise AppError("HITL_EXPIRED", "人工补充已过期", 409)
            payload = dict(row.payload)
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
        with self.database.sessions() as session:
            self.owned(session, claim)
            row = session.get(ChatRunOperation, (claim.run_id, key))
            if row:
                if row.input_hash != input_hash or row.readonly != readonly:
                    raise AppError("RUN_CHECKPOINT_DIVERGED", "检查点调用与已保存输入不一致", 409)
                if row.state == "succeeded":
                    return True, row.result
                if row.owner_generation == claim.generation and row.state == "started":
                    raise AppError("OPERATION_ALREADY_RUNNING", "同一检查点调用正在执行", 409)
                if not readonly:
                    row.state = "uncertain"
                    session.commit()
                    raise AppError("TOOL_OUTCOME_UNCERTAIN", "工具结果尚未确认，需人工核查后处理", 409)
                if row.attempts >= 3:
                    raise AppError("RUN_RECOVERY_LIMIT", "检查点调用恢复次数超过上限", 409)
                row.state, row.owner_generation, row.attempts = "started", claim.generation, row.attempts + 1
            else:
                count = session.scalar(select(func.count()).select_from(ChatRunOperation).where(ChatRunOperation.run_id == claim.run_id))
                if count >= 128:
                    raise AppError("RUN_CHECKPOINT_LIMIT", "运行检查点调用超过上限", 413)
                session.add(ChatRunOperation(run_id=claim.run_id, operation_key=key, input_hash=input_hash,
                    state="started", readonly=readonly, owner_generation=claim.generation, attempts=1))
            session.commit()
            return False, None

    def complete_operation(self, claim, key, result, *, checkpoint_payload=None):
        result = bounded_json(result, 2_000_000)
        with self.database.sessions() as session:
            run = self.owned(session, claim)
            row = session.get(ChatRunOperation, (claim.run_id, key))
            if not row or row.state != "started" or row.owner_generation != claim.generation:
                raise AppError("RUN_CHECKPOINT_DIVERGED", "检查点调用状态已变化", 409)
            row.state, row.result = "succeeded", result
            if checkpoint_payload is not None:
                self.save_checkpoint(session, run, checkpoint_payload)
            session.commit()

    def dispatch_outbox(self, limit=100):
        if not 1 <= limit <= 100:
            raise ValueError("Outbox dispatch limit must be 1..100")
        with self.database.sessions() as session:
            rows = session.scalars(select(ChatEventOutbox).where(ChatEventOutbox.dispatched_at.is_(None))
                .order_by(ChatEventOutbox.run_id, ChatEventOutbox.sequence).limit(limit).with_for_update(skip_locked=True)).all()
            for row in rows:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(select(func.pg_notify("rag_run_events", json.dumps({"run_id": str(row.run_id), "sequence": row.sequence}))))
                row.dispatched_at = self.now(session)
            session.commit()
            return len(rows)


class RunService:
    def __init__(self, settings, database, chat):
        self.store = RunStore(settings, database)
        from app.application.run_events import RunNotifications
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
