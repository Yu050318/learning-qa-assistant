from uuid import UUID
import logging

from app.application.auth import AuthService
from app.application.chat import ChatService
from app.application.ingestion import IngestionService
from app.application.memories import MemoryService
from app.application.retriever import RetrieverService
from app.application.sessions import SessionService
from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.embeddings.qwen import QwenEmbedding
from app.infrastructure.llms.providers import DeepSeekProvider, ModelRouter, OllamaProvider
from app.infrastructure.loaders.local import LocalDocumentLoader
from app.infrastructure.loaders.mineru import MinerUParser
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.search.scrapling import ScraplingWebReader
from app.infrastructure.search.tavily import TavilySearch
from app.infrastructure.vectorstores.milvus import MilvusVectorStore
from app.workflows.supervisor import SupervisorWorkflow
from app.infrastructure.sql import ReadOnlySQL
from app.infrastructure.rerank import TextReranker


class Services:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.database = Database(settings)
        self.auth = AuthService(settings, self.database)
        self.vectors = MilvusVectorStore(settings)
        self.embeddings = QwenEmbedding(settings)
        self.models = ModelRouter(settings, {"deepseek": DeepSeekProvider(settings), "ollama": OllamaProvider(settings)})
        self.reranker = TextReranker(settings) if settings.rerank_enabled else None
        self.retriever = RetrieverService(self.embeddings, self.vectors, settings.retrieval_top_k,
                                          context_expander=self.vectors.expand_context if settings.retrieval_parent_context else None,
                                          reranker=self.reranker)
        self.memories = MemoryService(settings, self.database, self.embeddings, self.vectors)
        self.web_search = TavilySearch(settings)
        self.web_reader = ScraplingWebReader(settings)
        self.mineru = MinerUParser(settings)
        self.sql = ReadOnlySQL(settings)
        self.agent_workflow = SupervisorWorkflow(
            settings, self.retriever, self.models, self.web_search, self.memories,
            web_reader=self.web_reader, sql=self.sql,
        )
        self.ingestion = IngestionService(settings, self.database, LocalDocumentLoader(settings), self.mineru, self.embeddings, self.vectors)
        self.sessions = SessionService(self.database)
        self.chat = ChatService(settings, self.database, self.sessions, self.agent_workflow)

    def start(self) -> None:
        try:
            recovered = self.ingestion.recover_pending()
            if recovered:
                logging.getLogger("rag").info("ingestion_recovered", extra={"document_count": recovered})
        except Exception as error:
            logging.getLogger("rag").warning("ingestion_recovery_unavailable", extra={"error_type": type(error).__name__})

    def readiness(self) -> dict:
        checks = {}
        for name, check in (("postgresql", self.database.check), ("milvus", self.vectors.check)):
            try:
                check()
                checks[name] = "ok"
            except AppError as error:
                checks[name] = error.code
            except Exception:
                checks[name] = "unavailable"
        configured = bool(self.settings.dashscope_api_key.get_secret_value())
        if self.settings.default_model_provider == "deepseek":
            configured = configured and bool(self.settings.deepseek_api_key.get_secret_value())
        checks["model_configuration"] = "ok" if configured else "missing"
        return {"status": "ready" if all(value == "ok" for value in checks.values()) else "not_ready", "checks": checks}

    def close(self) -> None:
        if self.reranker is not None:
            self.reranker.close()
        self.ingestion.close()
        self.web_search.close()
        self.mineru.close()
        self.sql.close()
        self.database.close()
