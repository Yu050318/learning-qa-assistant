"""Credential-free model profiles and validated execution-role assignments."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ModelRole = Literal["answer", "fast", "grader", "evaluator"]
ModelProvider = Literal["deepseek", "ollama"]
ProfileId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
MODEL_ROLES = ("answer", "fast", "grader", "evaluator")


class RegisteredModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    id: ProfileId
    version: int = Field(ge=1)
    display_name: str = Field(min_length=1, max_length=120)
    provider: ModelProvider
    # Connections resolve endpoint/credentials on the server. Browser edits
    # cannot redirect an existing server credential to a different URL.
    connection_id: ProfileId
    model_name: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9._:/-]+$")
    context_window: int = Field(ge=4096, le=2_000_000)
    max_output_tokens: int = Field(ge=1, le=16384)
    timeout_seconds: float = Field(gt=0, le=120)
    supports_stream: bool
    supports_tools: bool
    supports_structured_output: bool
    enabled: bool
    fallback_profile_id: ProfileId | None = None

    @model_validator(mode="after")
    def input_budget(self):
        if self.context_window < self.max_output_tokens + 1024:
            raise ValueError("Model context must reserve at least 1024 input tokens")
        return self

    def supports(self, role: ModelRole):
        return self.supports_stream and self.supports_tools if role == "answer" else self.supports_structured_output


class ModelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    profiles: dict[ProfileId, RegisteredModelProfile] = Field(min_length=1, max_length=64)
    assignments: dict[ModelRole, ProfileId]
    # Retain explicit provider selection for older clients without limiting
    # the catalog to one model per provider.
    provider_defaults: dict[ModelProvider, ProfileId]

    @model_validator(mode="after")
    def coherent(self):
        if set(self.assignments) != set(MODEL_ROLES):
            raise ValueError("All four model roles must be assigned")
        for key, profile in self.profiles.items():
            if key != profile.id:
                raise ValueError("Profile key must match its identity")
            if profile.fallback_profile_id is not None:
                fallback = self.profiles.get(profile.fallback_profile_id)
                if fallback is None or not fallback.enabled or fallback.id == profile.id or fallback.fallback_profile_id is not None:
                    raise ValueError("Fallback must reference an enabled profile with no further fallback")
        for role, key in self.assignments.items():
            profile = self.profiles.get(key)
            if profile is None or not profile.enabled or not profile.supports(role):
                raise ValueError(f"Role {role} requires an enabled profile with compatible capabilities")
            fallback = self.profiles.get(profile.fallback_profile_id)
            if fallback and not fallback.supports(role):
                raise ValueError(f"Fallback does not support role {role}")
        for provider, key in self.provider_defaults.items():
            profile = self.profiles.get(key)
            if profile is None or profile.provider != provider or not profile.enabled or not profile.supports("answer"):
                raise ValueError("Provider default must be an enabled answer-capable profile of that provider")
            fallback = self.profiles.get(profile.fallback_profile_id)
            if fallback and not fallback.supports("answer"):
                raise ValueError("Provider default fallback must support the answer role")
        return self

    def require(self, role: ModelRole) -> RegisteredModelProfile:
        return self.profiles[self.assignments[role]]
