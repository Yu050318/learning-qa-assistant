"""Published HTTP contracts, independent of controllers."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field
from app.entity.bo.capabilities import HttpToolDeclaration, SkillDeclaration, SqlPolicy, WebPolicy, SandboxPolicy

class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class SkillWrite(RevisionRequest):
    declaration: SkillDeclaration


class ToolWrite(RevisionRequest):
    declaration: HttpToolDeclaration


class SqlWrite(RevisionRequest):
    declaration: SqlPolicy


class WebWrite(RevisionRequest):
    declaration: WebPolicy


class SandboxWrite(RevisionRequest):
    declaration: SandboxPolicy
