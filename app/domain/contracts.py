from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    user_id: str
    document_id: str
    chunk_index: int
    content: str
    source_name: str
    page_number: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str


@dataclass(frozen=True)
class ModelAnswer:
    content: str
    provider: str
    model: str
    token_usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float | None
    retrieved_at: datetime
    published_at: datetime | None = None


@dataclass(frozen=True)
class WebSearchBatch:
    results: list[WebSearchResult]
    credits: float | None = None


@dataclass(frozen=True)
class AgentChatInput:
    question: str
    search_mode: Literal["knowledge", "web", "auto"] = "auto"
    document_ids: tuple[UUID, ...] | None = None
    web_query: str | None = None
    model_provider: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    answer: ModelAnswer
    kind: Literal["grounded", "smalltalk", "clarification", "insufficient"]
    citations: list[dict]
    search_mode: str
    run_metadata: dict


@dataclass(frozen=True)
class RunContext:
    request_id: str
    user_id: UUID
    session_id: UUID
    question: str
    search_mode: Literal["knowledge", "web", "auto"]
    allowed_document_ids: tuple[UUID, ...]
    selected_provider: str
    public_query: str | None
    web_enabled: bool
    warnings: tuple[str, ...] = ()
    started_at_utc: datetime | None = None
    deadline_monotonic: float = field(default_factory=lambda: monotonic() + 120)


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VectorStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk], vectors: list[list[float]]) -> None: ...
    def delete_document(self, user_id: UUID, document_id: UUID) -> None: ...
    def search(self, user_id: UUID, document_ids: list[UUID], vector: list[float], top_k: int) -> list[SearchHit]: ...
    def check(self) -> None: ...


class ChatModelProvider(Protocol):
    model_name: str

    def generate(self, messages: list[ChatTurn]) -> ModelAnswer: ...
    def stream(self, messages: list[ChatTurn]) -> Iterator[str]: ...
    def agent_client(self, timeout_seconds: float): ...
