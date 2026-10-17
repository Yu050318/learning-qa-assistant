"""Read-only deployment checks; results contain stable codes, never raw errors or secrets."""
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from app.application.model_center import ModelCenter
from app.application.workers import WorkerRegistry
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.infrastructure.persistence.models import DocumentRecord, User


def deployment_preflight(settings, database, vectors, *, require_worker=True):
    checks = []

    def record(name, status, code, action=""):
        checks.append({"name": name, "status": status, "code": code, "action": action})

    try:
        database.check()
        with database.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        if revision != ScriptDirectory.from_config(config).get_current_head():
            raise AppError("SCHEMA_UPGRADE_REQUIRED", "", 503)
        record("database", "pass", "SCHEMA_CURRENT")
    except Exception as error:
        record("database", "fail", error.code if isinstance(error, AppError) else "DATABASE_SCHEMA_UNAVAILABLE",
               "备份后执行 python -m app.migrate；不要使用 stamp 绕过结构核验")

    for name, path in (("upload_directory", settings.upload_dir), ("parsed_directory", settings.parsed_dir)):
        if path.is_dir() and os.access(path, os.R_OK | os.W_OK):
            record(name, "pass", "DIRECTORY_ACCESSIBLE")
        else:
            record(name, "fail", "DIRECTORY_NOT_ACCESSIBLE", "创建共享目录并为 API/Worker 配置一致的路径与读写权限")
    try:
        with database.sessions() as session:
            paths = session.scalars(select(DocumentRecord.file_path).where(DocumentRecord.status != "deleting"))
            from pathlib import Path
            valid_paths = all(Path(path).resolve().is_relative_to(settings.upload_dir.resolve()) and Path(path).is_file() for path in paths)
        record("document_files", "pass" if valid_paths else "fail", "DOCUMENT_FILES_ACCESSIBLE" if valid_paths else "DOCUMENT_FILES_NOT_ACCESSIBLE",
               "" if valid_paths else "核对旧文档实际文件与上传根目录；跨环境迁移需同步文件及受控路径映射，不能只复制数据库")
    except Exception:
        record("document_files", "fail", "DOCUMENT_FILES_UNAVAILABLE", "检查文档记录及共享存储路径")
    try:
        vectors.check()
        record("vectors", "pass", "VECTOR_SCHEMA_VALID")
    except Exception as error:
        record("vectors", "fail", error.code if isinstance(error, AppError) else "VECTOR_UNAVAILABLE",
               "核对 Milvus 连接、集合及模型维度；新环境显式执行 bootstrap --milvus")

    try:
        settings.require_auth_configuration()
        record("authentication", "pass", "AUTH_CONFIGURED")
    except ValueError:
        record("authentication", "fail", "AUTH_CONFIGURATION_REQUIRED", "配置至少 32 字节 JWT_SECRET")
    try:
        with database.sessions() as session:
            admins = session.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True)))
        record("platform_administrator", "pass" if admins else "warn", "ADMIN_AVAILABLE" if admins else "ADMIN_REGISTRATION_REQUIRED",
               "" if admins else "配置邀请码注册平台管理员，或显式迁移已核对的旧白名单")
    except Exception:
        record("platform_administrator", "fail", "ADMIN_ROLE_SCHEMA_UNAVAILABLE", "先完成数据库迁移")
    try:
        active = ModelCenter(settings, database).snapshot().settings if settings.model_center_enabled else settings
        configured = bool(settings.dashscope_api_key.get_secret_value()) and (
            active.default_model_provider == "ollama" or bool(active.deepseek_api_key.get_secret_value()))
        record("model_configuration", "pass" if configured else "fail", "MODEL_CONFIGURED" if configured else "MODEL_CREDENTIALS_MISSING",
               "配置状态不代表上游推理连通；使用合成资料单独验证实际调用")
    except Exception as error:
        record("model_configuration", "fail", error.code if isinstance(error, AppError) else "MODEL_CONFIGURATION_UNAVAILABLE",
               "执行 python -m app.bootstrap --model-center 并检查模型配置")
    if settings.ingestion_mode == "durable":
        try:
            count = WorkerRegistry(settings, database).healthy_count()
            record("ingestion_worker", "pass" if count else ("fail" if require_worker else "warn"),
                   "WORKER_AVAILABLE" if count else "WORKER_NOT_READY",
                   "" if count else "启动 python -m app.worker；API/Worker 的存储路径及处理配置必须一致")
        except Exception:
            record("ingestion_worker", "fail", "WORKER_REGISTRY_UNAVAILABLE", "先完成迁移，再启动独立 Worker")
    else:
        record("ingestion_worker", "warn", "IN_PROCESS_INGESTION", "切换 durable 模式并部署独立 Worker")
    if not settings.rerank_enabled:
        record("rerank", "disabled", "RERANK_DISABLED", "配置合法 endpoint/model/key 后再启用精排")
    return {"ready": not any(row["status"] == "fail" for row in checks), "checks": checks,
            "features": {name: getattr(settings, name) for name in (
                "model_center_enabled", "sharing_enabled", "document_versions_enabled", "chat_runs_enabled",
                "hierarchy_enabled", "knowledge_quality_enabled", "rerank_enabled")}}
