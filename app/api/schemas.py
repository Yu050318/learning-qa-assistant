from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


V2Citation = Annotated[Union[KnowledgeCitation, WebCitation], Field(discriminator="type")]


class V2ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    model_provider: Literal["deepseek", "ollama"] | None = None
    search_mode: Literal["knowledge", "web", "auto"] = "auto"
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    web_query: str | None = Field(default=None, min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.search_mode == "knowledge" and self.web_query is not None:
            raise ValueError("knowledge mode does not accept web_query")
        if self.search_mode == "web" and self.document_ids is not None:
            raise ValueError("web mode does not accept document_ids")
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
    search_mode: Literal["knowledge", "web", "auto"]
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
    search_mode: Literal["knowledge", "web", "auto"] = "auto"
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)
    web_query: str | None = Field(default=None, min_length=1, max_length=400)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.search_mode == "knowledge" and self.web_query is not None:
            raise ValueError("knowledge mode does not accept web_query")
        if self.search_mode == "web" and self.document_ids is not None:
            raise ValueError("web mode does not accept document_ids")
        if self.document_ids:
            self.document_ids = list(dict.fromkeys(self.document_ids))
        return self


class CompactRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    expected_context_version: str = Field(min_length=1, max_length=128)
    model_provider: Literal["deepseek", "ollama"] | None = None
