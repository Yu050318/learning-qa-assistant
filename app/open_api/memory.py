"""Public personal-memory contracts; independent of HTTP, storage and providers."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool


class MemorySettingsWrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    automatic_enabled: StrictBool


class MemorySettingsResponse(BaseModel):
    available: bool
    automatic_enabled: bool
    generation: int
    pending_jobs: int
    failed_jobs: int


class AutomaticMemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    content: str
    version: int
    source_session_id: UUID | None
    source_message_id: UUID | None
    updated_at: datetime
