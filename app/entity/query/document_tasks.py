"""Typed task filters shared by the use case and persistence layers."""
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class DocumentTaskQuery:
    user_id: UUID
    organization_id: UUID | None = None
    offset: int = 0
    limit: int = 50
    parent_task_id: UUID | None = None
    state: str | None = None
    kind: str | None = None
    search: str = ''
