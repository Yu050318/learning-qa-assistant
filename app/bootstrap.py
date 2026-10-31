import argparse

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.dao.postgres.database import Database
from app.dao.milvus import MilvusVectorStore
from app.service.model_center import ModelCenter


def main() -> int:
    parser = argparse.ArgumentParser(description="显式迁移平台 schema 和/或初始化 Milvus 集合；兼容的模块参数也执行完整平台迁移")
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--ingestion-jobs", action="store_true", help="兼容入口：执行完整平台迁移，准备持久化任务表")
    parser.add_argument("--chat-runs", action="store_true", help="兼容入口：执行完整平台迁移，准备 Run/Event 表")
    parser.add_argument("--sharing", action="store_true", help="兼容入口：执行完整平台迁移，准备组织共享表")
    parser.add_argument("--model-center", action="store_true", help="显式添加模型配置和调用记录表并从服务端配置初始化")
    parser.add_argument("--migrate-retired-deepseek-chat", action="store_true", help="显式将官方端点的旧 chat 模型迁移至 Flash，保留配置历史与角色分配")
    parser.add_argument("--hierarchy", action="store_true", help="兼容入口：执行完整平台迁移，不重建文档或向量")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--document-versions", action="store_true", help="兼容入口：执行完整平台迁移，准备文档版本表")
    parser.add_argument("--excel-provenance", action="store_true", help="显式为已有集合添加可空的 Excel JSON 来源字段（Milvus 2.6+）")
    options = parser.parse_args()
    if not any((options.postgres, options.milvus, options.excel_provenance, options.document_versions, options.ingestion_jobs, options.chat_runs, options.sharing, options.model_center, options.hierarchy, options.migrate_retired_deepseek_chat)):
        parser.error("请指定要执行的初始化或迁移选项，使用 --help 查看")
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
        if options.migrate_retired_deepseek_chat:
            result = ModelCenter(settings, database).migrate_retired_deepseek_chat()
            print(f"Model center: revision {result['revision']}; migrated profiles {result['migrated_profiles']}")
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
