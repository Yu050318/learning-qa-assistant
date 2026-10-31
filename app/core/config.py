import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr = Field(
        default=SecretStr(""), validation_alias=AliasChoices("DATABASE_URL", "DB_URL")
    )
    database_require_tls: bool = True
    database_timeout: int = Field(default=10, ge=1, le=120)
    milvus_uri: str = "http://localhost:19530"
    milvus_token: SecretStr = SecretStr("")
    milvus_database: str = "rag_tutorial"
    milvus_collection: str = "rag_docs_hybrid_v1_1024"
    milvus_timeout: float = Field(default=10, gt=0)
    dashscope_api_key: SecretStr = SecretStr("")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = Field(default=1024, ge=64, le=4096)
    embedding_batch_size: int = Field(default=10, ge=1, le=10)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_context_window: int = Field(default=64000, ge=4096)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_context_window: int = Field(default=32768, ge=4096)
    default_model_provider: Literal["deepseek", "ollama"] = "deepseek"
    allow_model_fallback: bool = False
    model_timeout: float = Field(default=60, gt=0)
    model_max_tokens: int = Field(default=2048, ge=1, le=16384)
    mineru_api_token: SecretStr = SecretStr("")
    mineru_enabled: bool = False
    mineru_model_version: str = "vlm"
    mineru_timeout: float = Field(default=15, gt=0)
    mineru_parse_timeout: float = Field(default=600, gt=0)
    ingestion_workers: int = Field(default=2, ge=1, le=8)
    ingestion_mode: Literal["in_process", "durable"] = "in_process"
    ingestion_lease_seconds: int = Field(default=120, ge=15, le=3600)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=10)
    ingestion_poll_seconds: float = Field(default=2, ge=0.1, le=60)
    parsed_dir: Path = PROJECT_ROOT / "data" / "parsed"
    web_search_enabled: bool = False
    tavily_api_key: SecretStr = SecretStr("")
    tavily_timeout: float = Field(default=15, gt=0)
    scrapling_timeout: float = Field(default=15, gt=0, le=60)
    scrapling_max_characters: int = Field(default=12000, ge=1000, le=50000)
    fixed_crawl_urls: list[str] = Field(default_factory=list)
    sql_database_url: SecretStr = SecretStr("")
    sql_allowed_tables: list[str] = Field(default_factory=list)
    sql_source_name: str = Field(default="业务数据库", min_length=1, max_length=100)
    sql_timeout: float = Field(default=5, gt=0, le=30)
    sql_max_rows: int = Field(default=100, ge=1, le=100)
    supervisor_max_delegations: int = Field(default=6, ge=1, le=6)
    supervisor_max_per_agent: int = Field(default=2, ge=1, le=2)
    agent_timeout: float = Field(default=120, gt=0)
    agent_max_model_calls: int = Field(default=16, ge=1, le=40)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=30)
    agent_max_knowledge_calls: int = Field(default=3, ge=1, le=20)
    knowledge_quality_enabled: bool = False
    document_versions_enabled: bool = False
    document_publication_enabled: bool = False
    chat_runs_enabled: bool = False
    sharing_enabled: bool = False
    workspaces_enabled: bool = False
    capability_center_enabled: bool = False
    tool_approvals_enabled: bool = False
    sandbox_enabled: bool = False
    sandbox_require_rootless: bool = True
    # Explicit Secret names only. Never expose arbitrary process environment
    # variables to a browser-defined HTTP endpoint.
    capability_secret_names: list[str] = Field(default_factory=list)
    model_center_enabled: bool = False
    evaluation_enabled: bool = False
    evaluation_queue_limit: int = Field(default=8, ge=1, le=100)
    evaluation_lease_seconds: int = Field(default=45, ge=15, le=3600)
    hierarchy_enabled: bool = False
    hierarchy_merge_threshold: float = Field(default=.6, ge=.5, le=1)
    model_admin_usernames: list[str] = Field(default_factory=list)
    enabled_model_providers: list[Literal["deepseek", "ollama"]] = Field(default_factory=lambda: ["deepseek", "ollama"])
    chat_run_workers: int = Field(default=4, ge=1, le=16)
    chat_run_queue_limit: int = Field(default=32, ge=1, le=1000)
    chat_run_lease_seconds: int = Field(default=45, ge=15, le=3600)
    chat_run_wait_seconds: int = Field(default=1800, ge=60, le=86400)
    agent_context_token_budget: int = Field(default=24000, ge=4000)
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr = SecretStr("")
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_project: str = "rag-backend"
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    langsmith_allow_custom_endpoint: bool = False
    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"
    max_upload_mb: int = Field(default=20, ge=1, le=100)
    max_document_characters: int = Field(default=2_000_000, ge=1)
    max_document_chunks: int = Field(default=5000, ge=1)
    chunk_size: int = Field(default=700, ge=100, le=2000)
    chunk_overlap: int = Field(default=100, ge=0)
    retrieval_top_k: int = Field(default=6, ge=1, le=30)
    retrieval_parent_context: bool = True
    retrieval_parent_group_size: int = Field(default=4, ge=2, le=8)
    rerank_enabled: bool = False
    rerank_endpoint: str = ""
    rerank_model: str = "gte-rerank-v2"
    rerank_api_key: SecretStr = SecretStr("")
    rerank_timeout: float = Field(default=5, gt=0, le=30)
    memory_recent_messages: int = Field(default=12, ge=2, le=40)
    libreoffice_path: str = "soffice"
    conversion_timeout: int = Field(default=60, ge=1, le=300)
    jwt_secret: SecretStr = SecretStr("")
    jwt_issuer: str = "learning-qa-assistant"
    jwt_audience: str = "learning-qa-api"
    access_token_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_days: int = Field(default=30, ge=1, le=90)
    registration_enabled: bool = False
    admin_invite_code: SecretStr = Field(default=SecretStr(""), repr=False)
    auth_cookie_secure: bool = True
    frontend_origin: str = "http://localhost:5173"
    memory_collection: str = "rag_user_memories_v1_1024"
    memory_top_k: int = Field(default=3, ge=1, le=10)
    memory_max_items: int = Field(default=200, ge=1, le=1000)
    openviking_enabled: bool = False
    openviking_url: str = "http://127.0.0.1:1933"
    openviking_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    openviking_account: str = "learning-qa"
    openviking_recall_timeout: float = Field(default=1, gt=0, le=5)
    openviking_worker_timeout: float = Field(default=15, gt=0, le=30)
    openviking_task_timeout: int = Field(default=600, ge=30, le=3600)
    openviking_poll_seconds: float = Field(default=2, ge=.1, le=60)

    @model_validator(mode="after")
    def check_chunk_overlap(self):
        if self.openviking_enabled:
            endpoint = urlsplit(self.openviking_url)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                    or endpoint.password or endpoint.query or endpoint.fragment
                    or not self.openviking_api_key.get_secret_value()
                    or not self.openviking_account or len(self.openviking_account) > 64
                    or not all(c.isalnum() or c in "-_" for c in self.openviking_account)):
                raise ValueError("OpenViking requires a trusted server URL, gateway key and account")
        if self.workspaces_enabled and not (self.sharing_enabled and self.document_publication_enabled
                and self.chat_runs_enabled and self.ingestion_mode == "durable"):
            raise ValueError("Workspaces require sharing, document publication, durable ingestion and durable runs")
        if self.document_publication_enabled and not self.document_versions_enabled:
            raise ValueError("Document publication requires document versions")
        if self.rerank_enabled:
            endpoint = urlsplit(self.rerank_endpoint)
            if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
                    or endpoint.port == 0 or endpoint.query or endpoint.fragment or not self.rerank_model.strip()
                    or not self.rerank_api_key.get_secret_value().strip()):
                raise ValueError("Enabled rerank requires an HTTPS endpoint, model and API key")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if not self.upload_dir.is_absolute():
            self.upload_dir = PROJECT_ROOT / self.upload_dir
        if not self.parsed_dir.is_absolute():
            self.parsed_dir = PROJECT_ROOT / self.parsed_dir
        return self

    def require_auth_configuration(self) -> None:
        if len(self.jwt_secret.get_secret_value().encode()) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")

    def configure_tracing(self) -> None:
        endpoint = urlsplit(self.langsmith_endpoint)
        official_hosts = {
            "api.smith.langchain.com", "eu.api.smith.langchain.com",
            "aws.api.smith.langchain.com", "apac.api.smith.langchain.com",
        }
        trusted = endpoint.scheme == "https" and (
            endpoint.hostname in official_hosts or self.langsmith_allow_custom_endpoint
        )
        enabled = self.langsmith_tracing and trusted
        if self.langsmith_tracing and not trusted:
            logging.getLogger("rag").warning("langsmith_disabled_untrusted_endpoint")
        for name in (
            "langsmith_tracing", "langsmith_api_key", "langsmith_endpoint",
            "langsmith_project", "langsmith_hide_inputs", "langsmith_hide_outputs",
        ):
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if isinstance(value, bool):
                value = str(value).lower()
            os.environ[name.upper()] = str(value)
        os.environ["LANGSMITH_TRACING"] = str(enabled).lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
