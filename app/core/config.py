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
    milvus_collection: str = "rag_docs_text_v4_1024"
    milvus_timeout: float = Field(default=10, gt=0)
    dashscope_api_key: SecretStr = SecretStr("")
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimension: int = Field(default=1024, ge=64, le=4096)
    embedding_batch_size: int = Field(default=10, ge=1, le=10)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
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
    parsed_dir: Path = PROJECT_ROOT / "data" / "parsed"
    web_search_enabled: bool = False
    tavily_api_key: SecretStr = SecretStr("")
    tavily_timeout: float = Field(default=15, gt=0)
    agent_timeout: float = Field(default=120, gt=0)
    agent_max_model_calls: int = Field(default=6, ge=1, le=20)
    agent_max_tool_calls: int = Field(default=4, ge=1, le=20)
    agent_max_knowledge_calls: int = Field(default=3, ge=1, le=20)
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
    memory_recent_messages: int = Field(default=12, ge=2, le=40)
    libreoffice_path: str = "soffice"
    conversion_timeout: int = Field(default=60, ge=1, le=300)

    @model_validator(mode="after")
    def check_chunk_overlap(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if not self.upload_dir.is_absolute():
            self.upload_dir = PROJECT_ROOT / self.upload_dir
        if not self.parsed_dir.is_absolute():
            self.parsed_dir = PROJECT_ROOT / self.parsed_dir
        return self

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
