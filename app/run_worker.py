"""Independent durable Run process: python -m app.run_worker [--once]."""
import argparse
import logging
import os
from pathlib import Path
import signal
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from uuid import UUID

from app.application.run_execution import RunWorker
from app.application.workers import WorkerRegistry
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.persistence.database import Database

logger = logging.getLogger("rag")


def main():
    parser = argparse.ArgumentParser(description="独立 Run Worker（不自动创建表）")
    parser.add_argument("--once", action="store_true", help="最多领取一个 Run 后退出")
    parser.add_argument("--healthcheck", action="store_true", help="只检查此进程实例的数据库心跳")
    options = parser.parse_args()
    configure_logging()
    settings = get_settings()
    services, database, registry, heartbeat_thread = None, None, None, None
    stop = Event()
    try:
        if not settings.chat_runs_enabled:
            raise AppError("CONFIGURATION_ERROR", "独立 Run Worker 要求 CHAT_RUNS_ENABLED=true", 503)
        marker = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/rag-run-worker-id"))
        if options.healthcheck:
            database = Database(settings)
            return 0 if WorkerRegistry(settings, database, kind="run").healthy_count(UUID(marker.read_text().strip())) else 1
        from app.application.container import Services
        services = Services(settings)
        database = services.database
        database.check()
        registry = WorkerRegistry(settings, database, kind="run")
        registry.beat()
        if os.environ.get("WORKER_HEARTBEAT_FILE"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(registry.worker_id))

        def heartbeat():
            while not stop.wait(10):
                try:
                    registry.beat()
                except Exception as error:
                    logger.warning("run_worker_heartbeat_failed", extra={"error_type": type(error).__name__})

        heartbeat_thread = Thread(target=heartbeat, name="run-worker-heartbeat", daemon=True)
        heartbeat_thread.start()
        worker = RunWorker(services.runs.store, services.chat)
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        def consume():
            while not stop.is_set():
                try:
                    worked = worker.run_once()
                    services.runs.store.dispatch_outbox()
                except Exception as error:
                    logger.error("run_worker_poll_failed", extra={"error_type": type(error).__name__})
                    if options.once:
                        return 1
                    worked = False
                if options.once:
                    return 0
                if not worked:
                    stop.wait(.5)

        if options.once:
            return consume()
        with ThreadPoolExecutor(max_workers=settings.chat_run_workers, thread_name_prefix="run-execution") as executor:
            slots = [executor.submit(consume) for _ in range(settings.chat_run_workers)]
            for slot in slots:
                slot.result()
        return 0
    except Exception as error:
        logger.error("run_worker_start_failed", extra={"error_type": type(error).__name__,
                     "error_code": error.code if isinstance(error, AppError) else "RUN_WORKER_START_FAILED"})
        return 1
    finally:
        stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=settings.database_timeout + 1)
        if registry:
            try:
                registry.stop()
            except Exception:
                logger.warning("run_worker_heartbeat_cleanup_failed")
        if services:
            services.close()
        elif database:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
