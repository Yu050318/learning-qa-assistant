import argparse

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.persistence.database import Database
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def main() -> int:
    parser = argparse.ArgumentParser(description="显式创建 RAG schema 和/或 Milvus 集合，不删除现有数据")
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--migrate-v2", action="store_true")
    options = parser.parse_args()
    if not options.postgres and not options.milvus and not options.migrate_v2:
        parser.error("请指定 --postgres、--milvus 和/或 --migrate-v2")
    configure_logging()
    settings = get_settings()
    database = Database(settings)
    try:
        if options.postgres:
            database.initialize()
            print("PostgreSQL: rag_v1 schema initialized")
        if options.postgres or options.migrate_v2:
            database.migrate_v2()
            print("PostgreSQL: agent-v2 migration applied")
        if options.milvus:
            MilvusVectorStore(settings).initialize()
            print("Milvus: configured collection initialized")
        return 0
    except AppError as error:
        print(f"Initialization failed: {error.code}: {error.message}")
        return 1
    except Exception as error:
        print(f"Initialization failed: {type(error).__name__}; check service configuration")
        return 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
