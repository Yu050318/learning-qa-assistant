"""Personal-memory values crossing service and retrieval boundaries."""
from typing import NotRequired, TypedDict
from uuid import UUID


class MemorySettingsState(TypedDict):
    available: bool
    automatic_enabled: bool
    generation: int
    pending_jobs: int
    failed_jobs: int


class MemoryHit(TypedDict):
    memory_id: UUID
    content: str
    version: int
    score: float
    asserted_at: NotRequired[str]
