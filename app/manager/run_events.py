"""PostgreSQL notifications only wake subscribers; SSE always reads the journal."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from threading import Event, Lock, Thread
from uuid import UUID
from app.dao.postgres.notifications import listen

logger = logging.getLogger("rag")


class RunNotifications:
    def __init__(self, store):
        self.store = store
        self.stopping = Event()
        self.lock = Lock()
        self.subscribers = {}
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopping.clear()
        self.thread = Thread(target=self.listen, name="run-event-notifications", daemon=True)
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=3)

    @asynccontextmanager
    async def subscribe(self, run_id):
        loop, signal = asyncio.get_running_loop(), asyncio.Event()
        subscription = (loop, signal)
        with self.lock:
            self.subscribers.setdefault(run_id, []).append(subscription)
        try:
            yield signal
        finally:
            with self.lock:
                listeners = self.subscribers.get(run_id, [])
                if subscription in listeners:
                    listeners.remove(subscription)
                if not listeners:
                    self.subscribers.pop(run_id, None)

    def wake(self, run_id):
        with self.lock:
            listeners = list(self.subscribers.get(run_id, []))
        for loop, signal in listeners:
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:
                pass  # A request may be closing its event loop during shutdown.

    def listen(self):
        while not self.stopping.is_set():
            connection = None
            try:
                connection = self.store.database.engine.raw_connection()
                raw = connection.driver_connection
                listen(raw)
                while not self.stopping.is_set():
                    self.store.dispatch_outbox()
                    for notice in raw.notifies(timeout=.5, stop_after=1):
                        try:
                            self.wake(UUID(json.loads(notice.payload)["run_id"]))
                        except (ValueError, KeyError, TypeError):
                            logger.warning("invalid_run_notification")
            except Exception as error:
                if not self.stopping.is_set():
                    logger.warning("run_notifications_unavailable", extra={"error_type": type(error).__name__})
                    self.stopping.wait(1)
            finally:
                if connection:
                    # A raw LISTEN connection has autocommit enabled. Discard it
                    # instead of returning those settings to transactional users.
                    connection.invalidate()
                    connection.close()
