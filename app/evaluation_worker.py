"""Independent evaluation process: python -m app.evaluation_worker [--once]."""
import argparse
import logging
import os
from pathlib import Path
import signal
from threading import Event, Thread
from uuid import UUID

from app.service.evaluation_worker import EvaluationWorker
from app.manager.workers import WorkerRegistry
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.dao.postgres.database import Database

logger = logging.getLogger("rag")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="处理至多一个评测任务后退出")
    parser.add_argument("--healthcheck", action="store_true", help="只检查本进程实例的数据库心跳")
    options = parser.parse_args()
    configure_logging()
    settings = get_settings()
    database, registry, thread = None, None, None
    stop = Event()
    try:
        if not settings.evaluation_enabled or not settings.model_center_enabled or not settings.chat_runs_enabled:
            raise AppError("EVALUATION_CONFIGURATION_REQUIRED", "评测 Worker 要求评测、模型中心及持久 Run 开启", 503)
        database = Database(settings)
        marker = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/rag-evaluation-worker-id"))
        if options.healthcheck:
            return 0 if WorkerRegistry(settings, database, kind="evaluation").healthy_count(UUID(marker.read_text().strip())) else 1
        database.check()
        registry = WorkerRegistry(settings, database, kind="evaluation")
        registry.beat()
        if os.environ.get("WORKER_HEARTBEAT_FILE"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(registry.worker_id))

        def heartbeat():
            while not stop.wait(10):
                try:
                    registry.beat()
                except Exception as error:
                    logger.warning("evaluation_worker_heartbeat_failed", extra={"error_type": type(error).__name__})

        thread = Thread(target=heartbeat, name="evaluation-worker-heartbeat", daemon=True)
        thread.start()
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        from app.composition.container import Services
        worker = EvaluationWorker(settings, database, Services)
        worker.stop = stop
        while not stop.is_set():
            try:
                worked = worker.run_once()
                worker.store.runs.dispatch_outbox()
            except Exception as error:
                logger.error("evaluation_worker_poll_failed", extra={"error_type": type(error).__name__})
                if options.once:
                    return 1
                worked = False
            if options.once:
                return 0
            if not worked:
                stop.wait(.5)
        return 0
    except Exception as error:
        logger.error("evaluation_worker_start_failed", extra={"error_type": type(error).__name__,
            "error_code": error.code if isinstance(error, AppError) else "EVALUATION_WORKER_START_FAILED"})
        return 1
    finally:
        stop.set()
        if thread:
            thread.join(timeout=settings.database_timeout + 1)
        if registry:
            try:
                registry.stop()
            except Exception:
                logger.warning("evaluation_worker_heartbeat_cleanup_failed")
        if database:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
