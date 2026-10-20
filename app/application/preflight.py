"""Read-only deployment checks; results contain stable codes, never raw errors or secrets."""
import os
import hashlib
from pathlib import Path
from uuid import UUID

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text, inspect, case

from app.application.model_center import ModelCenter
from app.application.workers import WorkerRegistry
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.application.document_publication import DocumentPublication
from app.domain.retrieval_scope import RetrievalIndex, index_connection_identity
from app.infrastructure.persistence.models import ChatRun, DocumentIndex, DocumentRecord, EvaluationJob, KnowledgeDocument, User


def frozen_scope_valid(settings, scope, ids_key, indexes_key):
    """Check stored inputs without rewriting or re-freezing an older run."""
    if not isinstance(scope, dict) or not isinstance(scope.get(ids_key), list):
        return False
    try:
        ids = [str(UUID(str(value))) for value in scope[ids_key]]
        values = scope.get(indexes_key, [])
        if not isinstance(values, list):
            return False
        targets = [RetrievalIndex.from_payload(value) for value in values]
    except (ValueError, TypeError, AttributeError):
        return False
    return (len(set(ids)) == len(ids) == len(targets) and {str(target.document_id) for target in targets} == set(ids)
        and all(target.connection_sha256 == index_connection_identity(settings)
            and target.database_name == settings.milvus_database for target in targets))


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
            paths = session.scalars(select(DocumentRecord.file_path).where(DocumentRecord.status.not_in(("deleting", "retired"))))
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

    if settings.document_publication_enabled:
        try:
            with database.sessions() as session:
                ready = session.scalars(select(DocumentRecord).where(DocumentRecord.status == "ready")).all()
                publication = DocumentPublication(settings, vectors)
                checked = {(settings.milvus_collection, settings.embedding_model, settings.embedding_dimension)}
                for document in ready:
                    index = publication.index(session, document, allow_bound_model=True)
                    if (index.state not in {"verified", "published", "superseded"} or not index.verified_at
                            or index.leaf_count != document.chunk_count or index.leaf_count < 1 or not index.leaf_sha256):
                        raise AppError("DOCUMENT_INDEX_NOT_VERIFIED", "", 409)
                    RetrievalIndex(user_id=document.user_id, **{name: getattr(index, name)
                        for name in RetrievalIndex.__dataclass_fields__ if name != "user_id"})
                    source_path = Path(document.file_path).resolve()
                    if not source_path.is_relative_to(settings.upload_dir.resolve()) or source_path == settings.upload_dir.resolve():
                        raise AppError("INVALID_STORAGE_PATH", "", 409)
                    with source_path.open("rb") as source:
                        if hashlib.file_digest(source, "sha256").hexdigest() != document.content_hash:
                            raise AppError("DOCUMENT_SOURCE_CHANGED", "", 409)
                    group = (index.collection_name, index.embedding_model, index.embedding_dimension)
                    if group not in checked:
                        vectors.for_collection(index.collection_name, embedding_model=index.embedding_model,
                            embedding_dimension=index.embedding_dimension).check()
                        checked.add(group)
            record("document_publication", "pass", "DOCUMENT_INDEX_BINDINGS_READY")
        except Exception as error:
            record("document_publication", "fail", error.code if isinstance(error, AppError) else "DOCUMENT_INDEX_BINDINGS_UNAVAILABLE",
                   "先迁移并核验旧源文件与索引；运行 python -m app.index_adoption，不要重写已有叶子或伪造绑定")
        for name, model, column, states, ids_key, indexes_key in (
            ("frozen_runs", ChatRun, ChatRun.execution_snapshot, ("queued", "running", "waiting", "cancelling"), "allowed_document_ids", "retrieval_indexes"),
            ("frozen_evaluations", EvaluationJob, EvaluationJob.scope_snapshot, ("queued", "running", "cancelling"), "document_ids", "indexes"),
        ):
            try:
                with database.sessions() as session:
                    query = select(column).where(model.state.in_(states))
                    if model is ChatRun:
                        query = query.where(ChatRun.expires_at > WorkerRegistry.now(session))
                    compatible = True
                    for snapshot in session.scalars(query):
                        scope = snapshot.get("context") if model is ChatRun and isinstance(snapshot, dict) else snapshot
                        if (not frozen_scope_valid(settings, scope, ids_key, indexes_key)
                                or model is EvaluationJob and not scope.get(ids_key)):
                            compatible = False
                            break
                record(name, "pass" if compatible else "fail", "FROZEN_INDEX_SCOPES_READY" if compatible else "LEGACY_FROZEN_SCOPE_REQUIRES_SETTLEMENT",
                       "" if compatible else "恢复原连接完成旧运行/评测或显式取消；不能改写旧检查点或用当前配置重新冻结")
            except Exception:
                record(name, "fail", "FROZEN_SCOPE_CHECK_UNAVAILABLE", "先完成迁移，再核查未结束的 Run 和评测")
    else:
        try:
            with database.sessions() as session:
                bound = inspect(session.connection()).has_table("document_indexes", schema="rag") and bool(session.scalar(
                    select(DocumentIndex.document_id).where(DocumentIndex.state != "cleaned").limit(1)))
            if bound:
                record("document_publication", "fail", "BOUND_INDEX_FEATURE_DISABLED", "已有冻结索引绑定，需要保持 DOCUMENT_PUBLICATION_ENABLED=true 并使用绑定检索")
        except Exception:
            record("document_publication", "fail", "DOCUMENT_INDEX_BINDINGS_UNAVAILABLE", "核查数据库迁移及已保存索引绑定")
    if settings.sharing_enabled:
        try:
            with database.sessions() as session:
                unselected = session.execute(select(KnowledgeDocument.knowledge_base_id, KnowledgeDocument.series_id).group_by(
                    KnowledgeDocument.knowledge_base_id, KnowledgeDocument.series_id).having(func.count() > 1,
                    func.sum(case((KnowledgeDocument.is_current.is_(True), 1), else_=0)) == 0).limit(1)).first()
            record("shared_version_selection", "warn" if unselected else "pass",
                   "SHARED_VERSION_SELECTION_REQUIRED" if unselected else "SHARED_VERSION_SELECTION_READY",
                   "核对共享库是否需明确发布当前版本；保持无当前版本也是有效选择，不自动回退历史" if unselected else "")
        except Exception:
            record("shared_version_selection", "fail", "SHARED_VERSION_CHECK_UNAVAILABLE", "先迁移并核查共享版本关系")

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
    if settings.chat_runs_enabled:
        try:
            count = WorkerRegistry(settings, database, kind="run").healthy_count()
            record("run_worker", "pass" if count else ("fail" if require_worker else "warn"),
                   "RUN_WORKER_AVAILABLE" if count else "RUN_WORKER_NOT_READY",
                   "" if count else "启动 python -m app.run_worker；确认 API/Worker 使用相同配置及平台数据库")
        except Exception:
            record("run_worker", "fail", "RUN_WORKER_REGISTRY_UNAVAILABLE", "完成迁移并启动独立 Run Worker")
    if settings.evaluation_enabled:
        try:
            configured = settings.model_center_enabled and settings.chat_runs_enabled
            count = WorkerRegistry(settings, database, kind="evaluation").healthy_count() if configured else 0
            record("evaluation_worker", "pass" if count else ("fail" if require_worker or not configured else "warn"),
                   "EVALUATION_WORKER_AVAILABLE" if count else "EVALUATION_WORKER_NOT_READY",
                   "" if count else "先迁移，再启用模型中心/持久 Run 并启动 python -m app.evaluation_worker")
        except Exception:
            record("evaluation_worker", "fail", "EVALUATION_WORKER_REGISTRY_UNAVAILABLE", "核对评测表迁移和独立 Worker 配置")
    if not settings.rerank_enabled:
        record("rerank", "disabled", "RERANK_DISABLED", "配置合法 endpoint/model/key 后再启用精排")
    return {"ready": not any(row["status"] == "fail" for row in checks), "checks": checks,
            "features": {name: getattr(settings, name) for name in (
                "model_center_enabled", "sharing_enabled", "document_versions_enabled", "chat_runs_enabled",
                "hierarchy_enabled", "knowledge_quality_enabled", "rerank_enabled", "evaluation_enabled",
                "document_publication_enabled", "workspaces_enabled")}}
