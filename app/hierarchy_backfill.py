"""Explicit, single-document upgrade. Does not call embeddings or mutate Milvus."""
import argparse
from uuid import UUID

from app.application.hierarchy import publish_tree
from app.core.config import get_settings
from app.core.errors import AppError
from app.infrastructure.loaders.hierarchy import HierarchyBuilder
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def backfill(settings, database, vectors, user_id, document_id):
    if not settings.hierarchy_enabled:
        raise AppError("HIERARCHY_DISABLED", "请先显式初始化节点表并启用 HIERARCHY_ENABLED", 503)
    with database.document_lock(document_id) as guard, database.sessions() as session:
        document = Repository(session).document(user_id, document_id, lock=True)
        if document.status != "ready" or document.embedding_model != settings.embedding_model:
            raise AppError("HIERARCHY_STALE", "仅支持当前向量模型下已就绪的文档", 409)
        guard()
        chunks = vectors.read_document_chunks(user_id, document_id, document.chunk_count)
        if any(c.source_name != document.original_name for c in chunks):
            raise AppError("HIERARCHY_SCOPE_MISMATCH", "切片文件名与文档记录不一致", 503)
        tree = HierarchyBuilder(settings).from_leaves(chunks)
        publish_tree(session, document, tree)
        guard()
        session.commit()
        return len(tree.nodes)


def main():
    parser = argparse.ArgumentParser(description="为单个已就绪文档补建层级，保留叶子 ID 和已有向量")
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--document-id", type=UUID, required=True)
    options = parser.parse_args()
    settings = get_settings()
    database = Database(settings)
    try:
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
