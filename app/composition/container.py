from uuid import UUID
import logging

from app.service.auth import AuthService
from app.service.artifacts import ArtifactService
from app.service.sandbox import SandboxService
from app.service.capabilities import CapabilityService
from app.manager.http_tools import HttpToolRuntime
from app.service.chat import ChatService
from app.service.ingestion import IngestionService
from app.service.memories import MemoryService
from app.service.automatic_memory import AutomaticMemoryService
from app.manager.openviking import OpenVikingManager
from app.manager.retrieval_catalog import build_retriever
from app.service.sessions import SessionService
from app.service.runs import RunService
from app.service.sharing import SharingService
from app.service.model_center import ModelCenter
from app.core.config import Settings
from app.core.errors import AppError
from app.manager.embeddings.qwen import QwenEmbedding
from app.manager.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
from app.manager.loaders.local import LocalDocumentLoader
from app.manager.loaders.mineru import MinerUParser
from app.dao.postgres.database import Database
from app.dao.postgres.repository import Repository
from app.manager.search.scrapling import ScraplingWebReader
from app.manager.search.tavily import TavilySearch
from app.dao.milvus import MilvusVectorStore
from app.service.agent.supervisor import SupervisorWorkflow
from app.manager.sql import ReadOnlySQL
from app.service.evaluation_jobs import EvaluationService


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings)
        self.auth = AuthService(settings, self.database)
        self.capabilities = CapabilityService(settings, self.database)
        self.http_tools = HttpToolRuntime(settings) if settings.capability_center_enabled else None
        self.vectors = MilvusVectorStore(settings)
        self.embeddings = QwenEmbedding(settings)
        self.models = ModelRouter(settings, {"deepseek": DeepSeekProvider(settings), "ollama": OllamaProvider(settings)})
        self.model_center = ModelCenter(settings, self.database) if settings.model_center_enabled else None
        self.evaluations = EvaluationService(settings, self.database)
        self.retriever = build_retriever(settings, self.database, vectors=self.vectors, embeddings=self.embeddings)
        self.openviking = OpenVikingManager(settings) if settings.openviking_enabled else None
        self.automatic_memories = AutomaticMemoryService(settings, self.database, self.openviking)
        self.memories = MemoryService(settings, self.database, self.embeddings, self.vectors, self.automatic_memories)
        self.web_search = TavilySearch(settings)
        self.web_reader = ScraplingWebReader(settings)
        self.mineru = MinerUParser(settings)
        self.sql = ReadOnlySQL(settings)
        self.sharing = SharingService(settings, self.database)
        self.agent_workflow = SupervisorWorkflow(
            settings, self.retriever, self.models, self.web_search, self.memories,
            web_reader=self.web_reader, sql=self.sql, sharing=self.sharing,
            capabilities=self.capabilities if settings.capability_center_enabled else None, http_tools=self.http_tools,
        )
        self.ingestion = IngestionService(settings, self.database, LocalDocumentLoader(settings), self.mineru, self.embeddings, self.vectors)
        self.sessions = SessionService(self.database)
        self.chat = ChatService(settings, self.database, self.sessions, self.agent_workflow)
        self.runs = RunService(settings, self.database, self.chat) if settings.chat_runs_enabled else None
        self.artifacts = ArtifactService(self.runs.store, self.capabilities) if self.runs is not None and settings.capability_center_enabled else None
        self.sandbox = SandboxService(settings, self.capabilities, self.artifacts) if self.artifacts is not None and settings.sandbox_enabled else None
        self.agent_workflow.sandbox = self.sandbox

    def start(self) -> None:
        if self.runs is not None:
            self.runs.notifications.start()
        try:
            recovered = self.ingestion.recover_pending()
            if recovered:
                logging.getLogger("rag").info("ingestion_recovered", extra={"document_count": recovered})
        except Exception as error:
            logging.getLogger("rag").warning("ingestion_recovery_unavailable", extra={"error_type": type(error).__name__})

    def readiness(self) -> dict:
        checks = {}
        if self.settings.capability_center_enabled:
            try:
                self.capabilities.snapshot()
                checks["capability_catalog"] = "ok" if self.settings.chat_runs_enabled else "durable_run_required"
            except Exception:
                checks["capability_catalog"] = "unavailable"
        for name, check in (("postgresql", self.database.check), ("milvus", self.vectors.check)):
            try:
                check()
                checks[name] = "ok"
            except AppError as error:
                checks[name] = error.code
            except Exception:
                checks[name] = "unavailable"
        configured = bool(self.settings.dashscope_api_key.get_secret_value())
        try:
            active = self.model_center.snapshot().settings if self.model_center else self.settings
            if active.default_model_provider == "deepseek":
                configured = configured and bool(active.deepseek_api_key.get_secret_value())
        except AppError:
            configured = False
        checks["model_configuration"] = "ok" if configured else "missing"
        if self.settings.ingestion_mode == "durable":
            from app.manager.workers import WorkerRegistry
            try:
                checks["ingestion_worker"] = "ok" if WorkerRegistry(self.settings, self.database).healthy_count() else "unavailable"
            except Exception:
                checks["ingestion_worker"] = "unavailable"
        if self.settings.chat_runs_enabled:
            from app.manager.workers import WorkerRegistry
            try:
                checks["run_worker"] = "ok" if WorkerRegistry(self.settings, self.database, kind="run").healthy_count() else "unavailable"
            except Exception:
                checks["run_worker"] = "unavailable"
        if self.settings.evaluation_enabled:
            from app.manager.workers import WorkerRegistry
            if not self.settings.model_center_enabled or not self.settings.chat_runs_enabled:
                checks["evaluation_worker"] = "configuration_required"
            else:
                try:
                    checks["evaluation_worker"] = "ok" if WorkerRegistry(self.settings, self.database, kind="evaluation").healthy_count() else "unavailable"
                except Exception:
                    checks["evaluation_worker"] = "unavailable"
        return {"status": "ready" if all(value == "ok" for value in checks.values()) else "not_ready", "checks": checks}

    def close(self) -> None:
        if self.openviking is not None:
            self.openviking.close()
        if self.http_tools is not None:
            self.http_tools.close()
        if self.runs is not None:
            self.runs.close()
        self.retriever.close()
        self.ingestion.close()
        self.web_search.close()
        self.mineru.close()
        self.sql.close()
        self.database.close()
