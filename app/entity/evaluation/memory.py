"""Sequence evaluation labels are never passed to the conversation or memory writer."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class MemoryScenario(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str
    family: str
    split: Literal['dev', 'frozen']
    category: Literal['preferences', 'facts', 'corrections', 'forgetting', 'unknown']
    initial_memories: list[str] = Field(default_factory=list)
    sessions: list[list[str]] = Field(min_length=1, max_length=3)
    forget_before_query: bool = False
    question: str
    expected_facts: list[str] = Field(default_factory=list)
    gold_memories: list[str] = Field(default_factory=list)
    forbidden_facts: list[str] = Field(default_factory=list)
    expected_abstain: bool = False


class MemoryDataset(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    schema_version: Literal[1] = 1
    name: str
    cases: list[MemoryScenario]

    @model_validator(mode='after')
    def independent_cases(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError('Duplicate memory case ID')
        development = {c.family for c in self.cases if c.split == 'dev'}
        frozen = {c.family for c in self.cases if c.split == 'frozen'}
        if development & frozen:
            raise ValueError('Development and frozen task families overlap')
        return self

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class MemoryReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str
    trial: int = Field(ge=1)
    group: Literal['baseline', 'openviking']
    reviewer: Literal['human', 'model']
    prediction_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    correct: StrictBool
    unsupported_personal_fact: StrictBool
    used_updated_fact: StrictBool | None = None


def prediction_digest(prediction):
    """Bind a review to its exact saved prediction, including model and evidence."""
    return hashlib.sha256(json.dumps(prediction, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()
