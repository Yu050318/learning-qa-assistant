"""Persist public SSE events; reconnects never execute the question again."""
import hashlib
import json
import logging
from dataclasses import asdict
from datetime import timedelta
from threading import BoundedSemaphore
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.application.ingestion_jobs import aware
from app.core.errors import AppError, not_found
from app.infrastructure.persistence.models import ChatRun, ChatRunEvent
from app.infrastructure.persistence.repository import Repository

logger = logging.getLogger("rag")


class RunStore:
    def __init__(self, settings, database):
        self.settings, self.database = settings, database

    def now(self, session):
        clock = func.clock_timestamp() if session.get_bind().dialect.name == "postgresql" else func.current_timestamp()
        return aware(session.scalar(select(clock)))

    def get_row(self, session, user_id, run_id, *, lock=False):
        query = select(ChatRun).where(ChatRun.id == run_id, ChatRun.user_id == user_id)
        run = session.scalar(query.with_for_update() if lock else query)
        if run is None:
            raise not_found()
        return run

    def guard(self, session, user_id, run_id):
        run = self.get_row(session, user_id, run_id, lock=True)
        if run.state != "running" or aware(run.expires_at) <= self.now(session):
            raise AppError("RUN_INTERRUPTED", "本轮问答已中断，请查看历史后重新提问", 409)
        return run

    def _event(self, session, run, event, data, *, terminal=False):
        payload = json.loads(json.dumps({**data, "run_id": str(run.id), "sequence": run.sequence + 1}, default=str))
        size = len(json.dumps(payload, ensure_ascii=False).encode())
        # Reserve room for a terminal error even if model deltas exhaust the budget.
        if size > 2_000_000 or run.sequence >= (8200 if terminal else 8192) or run.event_bytes + size > (8_000_000 if terminal else 6_000_000):
            raise AppError("RUN_EVENT_LIMIT", "本轮流式事件超过保存上限，请缩小问题范围", 413)
        run.sequence += 1
        run.event_bytes += size
        session.add(ChatRunEvent(run_id=run.id, sequence=run.sequence, event=event, payload=payload))

    def expire(self, session, run):
        if run.state == "running" and aware(run.expires_at) <= self.now(session):
            self._event(session, run, "error", {"code": "RUN_INTERRUPTED", "message": "服务执行中断或超时，请查看历史后重新提问", "status": 409}, terminal=True)
            run.state = "interrupted"

    def create(self, user_id, session_id, payload, request_id, client_key):
        inputs = asdict(payload)
        if not inputs.get("knowledge_base_ids"):
            inputs.pop("knowledge_base_ids", None)  # Preserve phase-nine keys across this additive request field.
        fingerprint = hashlib.sha256(json.dumps({"session_id": str(session_id), "input": inputs}, sort_keys=True, default=str).encode()).hexdigest()
        try:
            with self.database.sessions() as session:
                Repository(session).chat_session(user_id, session_id, lock=True)
                existing = session.scalar(select(ChatRun).where(ChatRun.user_id == user_id, ChatRun.client_key == client_key).with_for_update())
                if existing:
                    if existing.fingerprint != fingerprint:
                        raise AppError("IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同的问题或会话", 409)
                    self.expire(session, existing)
                    session.commit()
                    return existing, False
                active = session.scalar(select(ChatRun).where(ChatRun.session_id == session_id, ChatRun.state == "running").with_for_update())
                if active:
                    self.expire(session, active)
                    if active.state == "running":
                        raise AppError("RESOURCE_BUSY", "会话已有正在执行的问答", 409)
                    session.flush()
                run = ChatRun(id=uuid4(), user_id=user_id, session_id=session_id, client_key=client_key,
                              fingerprint=fingerprint, request_id=request_id, state="running", sequence=0, event_bytes=0,
                              expires_at=self.now(session) + timedelta(seconds=self.settings.agent_timeout + 30))
                session.add(run);session.flush()
                self._event(session, run, "run_started", {"request_id": request_id, "search_mode": payload.search_mode})
                session.commit()
                return run, True
        except IntegrityError:
            # Race on client key across different sessions: never start a second execution.
            raise AppError("IDEMPOTENCY_CONFLICT", "请求标识已使用，请用原标识查询结果", 409) from None

    def append(self, user_id, run_id, event, value):
        if event == "answer_delta":
            data = {"delta": value}
        elif event in {"agent_started", "agent_finished", "agent_failed"}:
            data = value
        elif event == "answer_start":
            data = {}
        else:
            raise AppError("RUN_EVENT_INVALID", "流式事件类型不受支持", 500)
        with self.database.sessions() as session:
            run = self.guard(session, user_id, run_id)
            self._event(session, run, event, data)
            session.commit()

    def complete(self, session, user_id, run_id, message):
        """Assistant message and terminal events commit together in ChatService."""
        run = self.guard(session, user_id, run_id)
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
            if after > run.sequence:
                raise AppError("INVALID_EVENT_CURSOR", "事件游标超过已保存的序号", 422)
            session.flush()
            events = session.scalars(select(ChatRunEvent).where(ChatRunEvent.run_id == run_id, ChatRunEvent.sequence > after)
                                     .order_by(ChatRunEvent.sequence).limit(100)).all()
            session.commit()
            return run, events


class RunService:
    def __init__(self, settings, database, chat):
        self.store = RunStore(settings, database)
        self.chat = chat
        self.slots = BoundedSemaphore(settings.chat_run_workers)
        self.executor = ThreadPoolExecutor(max_workers=settings.chat_run_workers, thread_name_prefix="chat-run")

    def close(self):
        self.executor.shutdown(wait=True)

    def start(self, user_id, session_id, payload, request_id, client_key):
        # Retry lookup must work even when all execution slots are occupied.
        with self.store.database.sessions() as session:
            existing = session.scalar(select(ChatRun).where(ChatRun.user_id == user_id, ChatRun.client_key == client_key))
        if existing:
            return self.store.create(user_id, session_id, payload, request_id, client_key)[0]
        if not self.slots.acquire(blocking=False):
            raise AppError("RUN_CAPACITY_EXCEEDED", "问答服务繁忙，请稍后用原请求标识重试", 429)
        try:
            run, created = self.store.create(user_id, session_id, payload, request_id, client_key)
            if created:
                try:
                    self.executor.submit(self.execute, run, payload)
                except Exception as error:
                    self.store.fail(user_id, run.id, error)
                    raise
                return run
        except Exception:
            self.slots.release()
            raise
        self.slots.release()
        return run

    def execute(self, run, payload):
        pending, last_flush = "", 0.0

        def flush():
            nonlocal pending, last_flush
            if pending:
                self.store.append(run.user_id, run.id, "answer_delta", pending)
                pending, last_flush = "", monotonic()

        def emit(event, value):
            nonlocal pending
            if event == "answer_delta":
                pending += value
                if len(pending) >= 512 or monotonic() - last_flush >= .2:
                    flush()
            else:
                flush()
                self.store.append(run.user_id, run.id, event, value)

        try:
            self.chat.answer_v2(run.user_id, run.session_id, payload, run.request_id,
                on_answer_event=emit,
                run_binding=(self.store, run.id, flush))
        except Exception as error:
            try:
                self.store.fail(run.user_id, run.id, error)
            except Exception as persistence_error:
                logger.error("chat_run_failure_not_saved", extra={"resource_id": str(run.id), "error_type": type(persistence_error).__name__})
        finally:
            self.slots.release()
