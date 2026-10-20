"""Run durable ingestion separately: python -m app.worker [--once]."""
import argparse
import logging
import signal
import os
from pathlib import Path
from uuid import UUID
from threading import Event, Thread

from app.application.ingestion import IngestionService
from app.application.ingestion_jobs import lease_lost
from app.application.workers import WorkerRegistry
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.embeddings.qwen import QwenEmbedding
from app.infrastructure.loaders.local import LocalDocumentLoader
from app.infrastructure.loaders.mineru import MinerUParser
from app.infrastructure.persistence.database import Database
from app.infrastructure.vectorstores.milvus import MilvusVectorStore

logger = logging.getLogger("rag")


class IngestionWorker:
    def __init__(self, ingestion):
        if ingestion.queue is None:
            raise AppError("CONFIGURATION_ERROR", "独立 Worker 要求 INGESTION_MODE=durable", 503)
        self.ingestion = ingestion
        self.queue = ingestion.queue

    def run_once(self):
        self.queue.reconcile()
        claim = self.queue.claim()
        if claim is None:
            return False
        stop, lost = Event(), Event()

        def heartbeat():
            while not stop.wait(self.ingestion.settings.ingestion_lease_seconds / 3):
                try:
                    self.queue.heartbeat(claim)
                except Exception:
                    lost.set()
                    return

        thread = None
        try:
            with self.ingestion.exclusive(claim.document_id) as probe:
                def guard():
                    if lost.is_set():
                        raise lease_lost()
                    probe()
                    self.queue.assert_owned(claim)

                guard()
                thread = Thread(target=heartbeat, name="ingestion-heartbeat", daemon=True)
                thread.start()
                error = None
                if not claim.exhausted:
                    try:
                        if claim.kind == "delete":
                            self.ingestion.tasks.process_retirement(claim.user_id, claim.document_id, guard=guard)
                        elif claim.kind == "hierarchy":
                            from app.hierarchy_backfill import backfill
                            backfill(self.ingestion.settings, self.ingestion.database, self.ingestion.vectors,
                                     claim.user_id, claim.document_id, guard=guard, already_locked=True)
                        else:
                            self.ingestion.process(claim.user_id, claim.document_id,
                                                   guard=guard, already_locked=True, raise_errors=True)
                    except Exception as failure:
                        error = failure
                guard()
                self.queue.finish(claim, error)
        except Exception as error:
            if isinstance(error, AppError) and error.code == "RESOURCE_BUSY":
                try:
                    self.queue.release_busy(claim)
                except Exception:
                    pass  # Expiry permits another worker to reclaim the job.
            logger.warning("ingestion_worker_attempt_interrupted", extra={
                "resource_id": str(claim.document_id), "error_type": type(error).__name__,
                "error_code": error.code if isinstance(error, AppError) else "INGESTION_WORKER_INTERRUPTED",
            })
        finally:
            stop.set()
            if thread:
                thread.join(timeout=self.ingestion.settings.database_timeout + 1)
        return True


def main():
    parser = argparse.ArgumentParser(description="独立 PostgreSQL 入库 Worker（不自动创建表）")
    parser.add_argument("--once", action="store_true", help="最多领取一个任务后退出")
    parser.add_argument("--healthcheck", action="store_true", help="检查此进程实例的数据库心跳；不领取任务")
    options = parser.parse_args()
    configure_logging()
    settings = get_settings()
    database, ingestion, mineru, registry, heartbeat_thread = Database(settings), None, None, None, None
    stop = Event()
    try:
        if settings.ingestion_mode != "durable":
            raise AppError("CONFIGURATION_ERROR", "请先设置 INGESTION_MODE=durable", 503)
        marker = Path(os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/rag-ingestion-worker-id"))
        if options.healthcheck:
            return 0 if WorkerRegistry(settings, database).healthy_count(UUID(marker.read_text().strip())) else 1
        database.check()
        registry = WorkerRegistry(settings, database)
        registry.beat()
        if os.environ.get("WORKER_HEARTBEAT_FILE"):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(registry.worker_id))

        def heartbeat():
            while not stop.wait(10):
                try:
                    registry.beat()
                except Exception as error:
                    logger.warning("worker_heartbeat_failed", extra={"error_type": type(error).__name__})

        heartbeat_thread = Thread(target=heartbeat, name="worker-pool-heartbeat", daemon=True)
        heartbeat_thread.start()
        mineru = MinerUParser(settings)
        ingestion = IngestionService(settings, database, LocalDocumentLoader(settings), mineru,
                                     QwenEmbedding(settings), MilvusVectorStore(settings))
        worker = IngestionWorker(ingestion)
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        while not stop.is_set():
            try:
                worked = worker.run_once()
            except Exception as error:
                logger.error("ingestion_worker_poll_failed", extra={"error_type": type(error).__name__})
                if options.once:
                    return 1
                worked = False
            if options.once:
                break
            if not worked:
                stop.wait(settings.ingestion_poll_seconds)
        return 0
    except Exception as error:
        logger.error("ingestion_worker_start_failed", extra={"error_type": type(error).__name__,
                     "error_code": error.code if isinstance(error, AppError) else "WORKER_START_FAILED"})
        return 1
    finally:
        stop.set()
        if heartbeat_thread:
            heartbeat_thread.join(timeout=settings.database_timeout + 1)
        if registry:
            try:
                registry.stop()
            except Exception:
                logger.warning("worker_heartbeat_cleanup_failed")
        if ingestion:
            ingestion.close()
        if mineru:
            mineru.close()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
