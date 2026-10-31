"""Published HTTP contracts, independent of controllers."""
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from app.core.errors import AppError

class Named(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class MemberWrite(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    username: str = Field(pattern=r"^[a-z0-9_-]{3,32}$")
    role: Literal["admin", "member"] = "member"


class GrantWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["viewer", "editor"]


class PublishVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_current_document_id: UUID | None
