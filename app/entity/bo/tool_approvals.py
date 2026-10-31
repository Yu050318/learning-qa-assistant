"""Names-only approval bound to a server-owned Run and frozen catalog."""
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.entity.bo.capabilities import ToolName


class RunToolApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: UUID = Field(repr=False)
    session_id: UUID = Field(repr=False)
    run_id: UUID = Field(repr=False)
    organization_id: UUID | None = Field(repr=False)
    catalog_hash: str = Field(pattern=r"^[a-f0-9]{64}$", repr=False)
    tool_names: tuple[ToolName, ...] = Field(min_length=1, max_length=131, strict=False, repr=False)

    @model_validator(mode="after")
    def unique_tools(self):
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("Approved tool identities must be unique")
        return self

    def allows(self, tool_name, *, user_id, session_id, run_id, organization_id, catalog_hash):
        return (tool_name in self.tool_names
            and (self.user_id, self.session_id, self.run_id, self.organization_id, self.catalog_hash)
                == (user_id, session_id, run_id, organization_id, catalog_hash))

    def __repr__(self):
        return f"RunToolApproval(tool_count={len(self.tool_names)}, bound=True)"


def validate_run_approval(raw, tool_names, **identity):
    if raw is None:
        if tool_names:
            raise ValueError("Approved tools require a bound grant")
        return None
    # Revalidate typed models too: model_copy may bypass normal validation.
    grant = RunToolApproval.model_validate_json(raw.model_dump_json(warnings=False)) if isinstance(raw, RunToolApproval) else RunToolApproval.model_validate_json(json.dumps(raw))
    if set(grant.tool_names) != set(tool_names) or any(not grant.allows(name, **identity) for name in grant.tool_names):
        raise ValueError("Approval differs from the Run identity or tool scope")
    return grant
