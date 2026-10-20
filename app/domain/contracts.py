from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Literal, Protocol
from uuid import UUID
from urllib.parse import quote
import hashlib
import json
from dataclasses import asdict
from app.domain.retrieval_scope import RetrievalIndex
from uuid import uuid5


@dataclass(frozen=True)
class ExcelSource:
    sheet_name: str
    row_start: int
    row_end: int
    source_anchor: str

    def __post_init__(self):
        valid = isinstance(self.sheet_name, str) and 1 <= len(self.sheet_name) <= 31
        valid = valid and type(self.row_start) is int and type(self.row_end) is int and 2 <= self.row_start <= self.row_end <= 10000
        expected = f"sheet:{quote(self.sheet_name, safe='')}:row:{self.row_start}" if valid else ""
        if valid and self.row_end != self.row_start:
            expected += f"-{self.row_end}"
        if not valid or self.source_anchor != expected:
            raise ValueError("Invalid Excel source")


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
    excel_source: ExcelSource | None = None


@dataclass(frozen=True)
class ParentContext:
    """Unnumbered context built from persisted children in one source boundary."""
    children: tuple[Chunk, ...]

    def __post_init__(self):
        if not 2 <= len(self.children) <= 8 or len(self.content) > 12000:
            raise ValueError("Parent context exceeds bounds")
        first = self.children[0]
        positions = [child.chunk_index for child in self.children]
        if any(type(index) is not int or index < 0 for index in positions) or positions != sorted(set(positions)):
            raise ValueError("Invalid child positions")
        if len({child.chunk_id for child in self.children}) != len(self.children):
            raise ValueError("Duplicate child identities")
        if any(self.boundary(child) != self.boundary(first) for child in self.children):
            raise ValueError("Parent crosses a source boundary")

    @staticmethod
    def boundary(chunk: Chunk):
        return (chunk.user_id, chunk.document_id, chunk.source_name, chunk.page_number, chunk.section,
                chunk.excel_source.sheet_name if chunk.excel_source else None)

    @property
    def content(self) -> str:
        return "\n\n".join(child.content for child in self.children)

    @property
    def context_id(self) -> str:
        digest = hashlib.sha256(json.dumps([asdict(child) for child in self.children], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return str(uuid5(UUID(self.children[0].document_id), f"parent-context:v1:{digest}"))

    def contains(self, leaf: Chunk) -> bool:
        return leaf in self.children


@dataclass(frozen=True)
class HierarchyContext:
    node_id: str
    level: int
    text: str
    children: tuple[Chunk, ...]

    def __post_init__(self):
        UUID(self.node_id)
        if self.level not in {1, 2} or not 2 <= len(self.children) <= 64 or not 1 <= len(self.text) <= 48000:
            raise ValueError("Invalid hierarchy context")
        first = self.children[0]
        positions = [c.chunk_index for c in self.children]
        if any(type(i) is not int or i < 0 for i in positions) or positions != sorted(set(positions)):
            raise ValueError("Invalid hierarchy positions")
        if (len({child.chunk_id for child in self.children}) != len(self.children)
                or any(ParentContext.boundary(child) != ParentContext.boundary(first) or child.content not in self.text for child in self.children)):
            raise ValueError("Hierarchy crosses a source boundary")

    @property
    def content(self):
        return self.text

    @property
    def context_id(self):
        return self.node_id

    def contains(self, leaf):
        return leaf in self.children


@dataclass(frozen=True)
class RerankTrace:
    status: Literal["applied", "fallback"]
    model: str
    candidate_count: int
    elapsed_ms: float
    attempts: int
    input_truncated: bool = False
    error_code: str | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    score: float
    parent_context: ParentContext | HierarchyContext | None = None
    context_warning: str | None = None
    rerank_score: float | None = None
    rerank_trace: RerankTrace | None = None
    hierarchy_trace: dict | None = None


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
    search_mode: Literal["knowledge", "web", "sql", "auto"] = "auto"
    document_ids: tuple[UUID, ...] | None = None
    web_query: str | None = None
    model_provider: str | None = None
    web_urls: tuple[str, ...] = ()
    knowledge_base_ids: tuple[UUID, ...] = ()
    model_profile_id: str | None = None


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
    search_mode: Literal["knowledge", "web", "sql", "auto"]
    allowed_document_ids: tuple[UUID, ...]
    selected_provider: str
    public_query: str | None
    web_enabled: bool
    web_urls: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_message_id: UUID | None = None
    started_at_utc: datetime | None = None
    deadline_monotonic: float = field(default_factory=lambda: monotonic() + 120)
    knowledge_base_ids: tuple[UUID, ...] = ()
    document_owners: tuple[tuple[UUID, UUID], ...] = ()
    retrieval_indexes: tuple[RetrievalIndex, ...] = ()
    organization_id: UUID | None = None
    shared_only: bool = False

    def __post_init__(self):
        if self.organization_id is not None and not isinstance(self.organization_id, UUID):
            raise ValueError("Workspace identity must be a UUID")
        if type(self.shared_only) is not bool or self.organization_id is not None and not self.shared_only:
            raise ValueError("Team contexts require a shared-only scope")
        if not isinstance(self.retrieval_indexes, tuple) or any(not isinstance(target, RetrievalIndex) for target in self.retrieval_indexes):
            raise ValueError("Retrieval indexes must be an immutable tuple")
        if self.retrieval_indexes:
            owners = dict(self.document_owners) or {did: self.user_id for did in self.allowed_document_ids}
            if (len(self.retrieval_indexes) != len(set(self.allowed_document_ids)) or set(owners) != set(self.allowed_document_ids)
                    or {target.document_id: target.user_id for target in self.retrieval_indexes} != owners):
                raise ValueError("Retrieval index scope does not match authorized documents")


class EmbeddingProvider(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class VectorStore(Protocol):
    def upsert(self, chunks: Sequence[Chunk], vectors: list[list[float]]) -> None: ...
    def delete_document(self, user_id: UUID, document_id: UUID) -> None: ...
    def search(self, user_id: UUID, document_ids: list[UUID], query: str, vector: list[float], top_k: int) -> list[SearchHit]: ...
    def check(self) -> None: ...


class ChatModelProvider(Protocol):
    model_name: str

    def generate(self, messages: list[ChatTurn]) -> ModelAnswer: ...
    def stream(self, messages: list[ChatTurn]) -> Iterator[str]: ...
    def agent_client(self, timeout_seconds: float): ...
