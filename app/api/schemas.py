from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.infrastructure.search.tavily import safe_public_url


def validate_web_scope(payload):
    if payload.search_mode in {"knowledge", "sql"} and (payload.web_query is not None or payload.web_urls):
        raise ValueError("当前模式不接受联网参数")
    if payload.search_mode in {"web", "sql"} and payload.document_ids is not None:
        raise ValueError("当前模式不接受知识库文档")
    if any(len(url) > 2048 or not safe_public_url(url) for url in payload.web_urls):
        raise ValueError("网页地址无效或不是公网 HTTP(S) 地址")
    payload.web_urls = list(dict.fromkeys(payload.web_urls))


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=30)


class UserResponse(ORMResponse):
    id: UUID
    username: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserResponse


class MemoryWrite(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(min_length=1, max_length=500)


class MemoryResponse(ORMResponse):
    id: UUID
    content: str
    status: str
    version: int
    created_via: str
    source_session_id: UUID | None
    source_message_id: UUID | None
    vector_updated_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentResponse(ORMResponse):
    id: UUID
    original_name: str
    file_type: str
    status: str
    chunk_count: int
    embedding_model: str
    error_message: str | None
    size_bytes: int | None = Field(default=None, ge=0)
    can_query: bool = False
    unavailable_reason: Literal["DOCUMENT_NOT_READY", "EMBEDDING_MODEL_MISMATCH", "DOCUMENT_DELETING"] | None = None
    created_at: datetime
    updated_at: datetime


class SessionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(default="新会话", min_length=1, max_length=200)


class SessionRename(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=200)


class SessionResponse(ORMResponse):
    id: UUID
    title: str
    summary: str
    summarized_through: UUID | None
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    number: int
    document_id: UUID
    chunk_id: UUID
    source_name: str
    page_number: int | None
    section: str | None
    excerpt: str
    score: float


class KnowledgeCitation(Citation):
    model_config = ConfigDict(extra="forbid")
    type: Literal["knowledge"] = "knowledge"


class WebCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["web"] = "web"
    number: int = Field(ge=1)
    title: str
    url: str
    excerpt: str = Field(max_length=500)
    retrieved_at: datetime
    published_at: datetime | None = None
    score: float | None = None


class MemoryCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["memory"] = "memory"
    number: int = Field(ge=1)
    memory_id: UUID
    excerpt: str = Field(max_length=500)
    score: float


class SQLCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["sql"] = "sql"
    number: int = Field(ge=1)
    source_name: str
    query: str
    columns: list[str]
    row_count: int = Field(ge=0)
    truncated: bool
    excerpt: str = Field(max_length=500)


V2Citation = Annotated[Union[KnowledgeCitation, WebCitation, MemoryCitation, SQLCitation], Field(discriminator="type")]


class V2ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    model_provider: Literal["deepseek", "ollama"] | None = None
    search_mode: Literal["knowledge", "web", "sql", "auto"] = "auto"
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    web_query: str | None = Field(default=None, min_length=1, max_length=400)
    web_urls: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_scope(self):
        validate_web_scope(self)
        if self.document_ids is not None:
            self.document_ids = list(dict.fromkeys(self.document_ids))
        return self


class V2MessageResponse(ORMResponse):
    id: UUID
    role: str
    content: str
    model_provider: str | None
    model_name: str | None
    citations: list[V2Citation]
    token_usage: dict | None
    run_metadata: dict | None
    created_at: datetime


class V2SessionDetail(SessionResponse):
    messages: list[V2MessageResponse]


class V2ChatResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    message_id: UUID = Field(validation_alias="id")
    answer: str = Field(validation_alias="content")
    model_provider: str
    model_name: str
    search_mode: Literal["knowledge", "web", "sql", "auto"]
    citations: list[V2Citation]
    token_usage: dict | None
    run_metadata: dict


class DocumentPage(BaseModel):
    items: list[DocumentResponse]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class ContextEstimateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: UUID | None = None
    question: str = Field(default="", max_length=8000)
    model_provider: Literal["deepseek", "ollama"] | None = None
    search_mode: Literal["knowledge", "web", "sql", "auto"] = "auto"
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    web_query: str | None = Field(default=None, min_length=1, max_length=400)
    web_urls: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_scope(self):
        validate_web_scope(self)
        if self.document_ids:
            self.document_ids = list(dict.fromkeys(self.document_ids))
        return self


class CompactRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    expected_context_version: str = Field(min_length=1, max_length=128)
    model_provider: Literal["deepseek", "ollama"] | None = None
