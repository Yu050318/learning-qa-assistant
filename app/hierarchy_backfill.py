from app.service.hierarchy_backfill import backfill, enqueue, require_mutable_index
"""Explicit, single-document upgrade. Does not call embeddings or mutate Milvus."""
import argparse
from contextlib import nullcontext
from uuid import UUID
from sqlalchemy import inspect

from app.manager.hierarchy import publish_tree
from app.core.config import get_settings
from app.core.errors import AppError
from app.manager.loaders.hierarchy import HierarchyBuilder
from app.dao.postgres.database import Database
from app.dao.postgres.repository import Repository
from app.dao.milvus import MilvusVectorStore
from app.entity.do.models import DocumentIndex, DocumentTask, IngestionJob
from app.service.ingestion_jobs import IngestionQueue








def main():
    parser = argparse.ArgumentParser(description="为单个已就绪文档补建层级，保留叶子 ID 和已有向量")
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    parser.add_argument("--enqueue", action="store_true", help="提交持久任务，由独立 Worker 执行并按已有租约策略重试")
    options = parser.parse_args()
    settings = get_settings()
    database = Database(settings)
    try:
        if options.enqueue:
            enqueue(settings, database, options.user_id, options.document_id)
            print("Hierarchy backfill queued; existing vectors unchanged")
            return 0
        count = backfill(settings, database, MilvusVectorStore(settings), options.user_id, options.document_id)
        print(f"Hierarchy published: {count} nodes; original leaf IDs/vectors preserved")
        return 0
    except AppError as error:
        print(f"Backfill failed: {error.code}: {error.message}")
        return 1
    except Exception as error:
        print(f"Backfill failed: {type(error).__name__}; check service configuration")
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
