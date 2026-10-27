"""Bounded input and server-owned controls for one isolated execution."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def validate_image_reference(value):
    if not isinstance(value, str) or re.fullmatch(r"(?:sha256:[a-f0-9]{64}|[a-z0-9][a-z0-9./:_-]{0,255}@sha256:[a-f0-9]{64})", value) is None:
        raise ValueError("Sandbox image must be an immutable digest, never a moving tag")
    return value


class SandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    language: Literal["python", "sh"]
    source: str = Field(min_length=1, max_length=65536, repr=False)

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value):
        if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > 65536:
            raise ValueError("Sandbox source must be nonblank, UTF-8 and at most 64 KiB")
        return value

    @property
    def input_hash(self):
        raw = json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SandboxLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    timeout_seconds: float = Field(default=15.0, ge=.1, le=30, allow_inf_nan=False)
    memory_bytes: int = Field(default=134217728, ge=67108864, le=536870912)
    cpu_count: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    pids_limit: int = Field(default=32, ge=8, le=64)
    output_bytes: int = Field(default=65536, ge=1024, le=65536)
    workspace_bytes: int = Field(default=16777216, ge=1048576, le=67108864)
    max_files: int = Field(default=8, ge=1, le=16)
    max_file_bytes: int = Field(default=1048576, ge=1024, le=2097152)
    max_artifact_bytes: int = Field(default=2097152, ge=1024, le=4194304)

    @model_validator(mode="after")
    def coherent(self):
        if not self.max_file_bytes <= self.max_artifact_bytes <= self.workspace_bytes:
            raise ValueError("Artifact budgets must fit the temporary workspace")
        return self


class SandboxExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    invocation_id: UUID = Field(repr=False)
    run_id: UUID = Field(repr=False)
    request: SandboxRequest = Field(repr=False)
    image: str = Field(repr=False)
    limits: SandboxLimits

    @field_validator("image")
    @classmethod
    def pinned_image(cls, value):
        return validate_image_reference(value)

    @property
    def container_name(self):
        return "learningqa-sandbox-" + self.invocation_id.hex


@dataclass(frozen=True)
class SandboxFile:
    name: str
    content: bytes = field(repr=False)

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class SandboxResult:
    success: bool
    error_code: str | None
    exit_code: int | None
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    files: tuple[SandboxFile, ...] = field(repr=False)
