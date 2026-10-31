from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
Provider = Literal["deepseek","ollama"]

class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model_name: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9._:/-]+$")
    context_window: int = Field(ge=4096, le=2_000_000)
    enabled: bool


class ManagedModels(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: dict[Provider, ModelProfile]
    default_provider: Provider
    allow_fallback: bool
    timeout_seconds: float = Field(gt=0, le=120)
    max_output_tokens: int = Field(ge=1, le=16384)

    @model_validator(mode="after")
    def valid(self):
        if set(self.profiles) != {"deepseek", "ollama"}:
            raise ValueError("Both existing provider profiles are required")
        if not self.profiles[self.default_provider].enabled:
            raise ValueError("Default provider must be enabled")
        if self.allow_fallback and not all(profile.enabled for profile in self.profiles.values()):
            raise ValueError("Fallback requires both profiles enabled")
        if any(profile.context_window < self.max_output_tokens + 1024 for profile in self.profiles.values()):
            raise ValueError("Context window must reserve input space beyond output tokens")
        return self
