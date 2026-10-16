import argparse

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.infrastructure.persistence.database import Database
from app.infrastructure.vectorstores.milvus import MilvusVectorStore
from app.application.model_center import ModelCenter


def main() -> int:
    parser = argparse.ArgumentParser(description="显式创建 RAG schema 和/或 Milvus 集合")
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--ingestion-jobs", action="store_true", help="显式添加持久化入库任务表")
    parser.add_argument("--chat-runs", action="store_true", help="显式添加问答 Run/Event 表")
    parser.add_argument("--sharing", action="store_true", help="显式添加组织与知识库共享表")
    parser.add_argument("--model-center", action="store_true", help="显式添加模型配置和调用记录表并从服务端配置初始化")
    parser.add_argument("--hierarchy", action="store_true", help="显式添加三级分块节点表；不重建原有文档或向量")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--document-versions", action="store_true", help="显式为已有平台库添加文档版本表，不更改或删除原文档")
    parser.add_argument("--excel-provenance", action="store_true", help="显式为已有集合添加可空的 Excel JSON 来源字段（Milvus 2.6+）")
    options = parser.parse_args()
    if not any((options.postgres, options.milvus, options.excel_provenance, options.document_versions, options.ingestion_jobs, options.chat_runs, options.sharing, options.model_center, options.hierarchy)):
        parser.error("请指定 --postgres、--milvus、--excel-provenance、--document-versions、--ingestion-jobs、--chat-runs、--sharing、--model-center 或 --hierarchy")
    configure_logging()
    settings = get_settings()
    database = Database(settings)
    try:
        if options.postgres:
            database.initialize()
            print("PostgreSQL: rag schema initialized")
        if options.ingestion_jobs:
            database.initialize_ingestion_jobs()
            print("PostgreSQL: durable ingestion jobs ready")
        if options.chat_runs:
            database.initialize_chat_runs()
            print("PostgreSQL: durable chat run/event tables ready")
        if options.sharing:
            database.initialize_sharing()
            print("PostgreSQL: organization and knowledge sharing tables ready")
        if options.model_center:
            database.initialize_model_center()
            ModelCenter(settings, database).initialize()
            print("PostgreSQL: model center initialized without overwriting existing configuration")
        if options.hierarchy:
            database.initialize_hierarchy()
            print("PostgreSQL: additive hierarchy table ready; existing vectors unchanged")
        if options.milvus:
            MilvusVectorStore(settings).initialize()
            print("Milvus: configured collection initialized")
        if options.document_versions:
            database.initialize_document_versions()
            print("PostgreSQL: additive document version tables ready")
        if options.excel_provenance:
            MilvusVectorStore(settings).add_excel_provenance()
            print("Milvus: nullable Excel provenance field ready")
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
