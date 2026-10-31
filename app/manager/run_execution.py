"""One durable execution slot; the API only prepares and queues Runs."""
import logging
from threading import Event, Thread
from time import monotonic

from app.core.errors import AppError

logger = logging.getLogger("rag")


class RunWorker:
    def __init__(self, store, chat):
        self.store, self.chat = store, chat

    def run_once(self):
        claim = self.store.claim_next()
        if claim is None:
            return False
        stop = Event()

        def heartbeat():
            while not stop.wait(self.store.settings.chat_run_lease_seconds / 3):
                try:
                    self.store.heartbeat(claim)
                except Exception:
                    # Fencing remains authoritative at every call and write.
                    return

        thread = Thread(target=heartbeat, name="run-lease-heartbeat", daemon=True)
        thread.start()
        pending, last_flush = "", 0.0

        def flush():
            nonlocal pending, last_flush
            if pending:
                self.store.append(claim.user_id, claim.run_id, "answer_delta", pending, claim=claim)
                pending, last_flush = "", monotonic()

        def emit(event, value):
            nonlocal pending
            if event == "answer_delta":
                pending += value
                if len(pending) >= 512 or monotonic() - last_flush >= .2:
                    flush()
            else:
                flush()
                self.store.append(claim.user_id, claim.run_id, event, value, claim=claim)

        try:
            self.chat.execute_prepared(self.store, claim, on_answer_event=emit, flush_answer=flush)
        except Exception as error:
            try:
                if isinstance(error, AppError) and error.code == "RESOURCE_BUSY":
                    self.store.release_busy(claim)
                else:
                    self.store.finish_failure(claim, error)
            except Exception as persistence_error:
                logger.warning("run_attempt_not_settled", extra={"resource_id": str(claim.run_id),
                               "error_type": type(persistence_error).__name__})
        finally:
            stop.set()
            thread.join(timeout=self.store.settings.database_timeout + 1)
        return True
