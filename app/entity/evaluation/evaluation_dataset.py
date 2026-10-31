"""Validated RAG workbench labels, kept separate from ingestible documents."""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from app.entity.evaluation.evaluation import AnswerCase, StrictModel, digest


class GoldAnchor(StrictModel):
    canonical_name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\]+\.(?:xlsx|html|pdf|docx|txt|md)$")
    sheet: str | None = Field(default=None, max_length=100)
    row_start: int | None = Field(default=None, ge=1)
    row_end: int | None = Field(default=None, ge=1)
    section: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def complete_range(self):
        if (self.row_start is None) != (self.row_end is None):
            raise ValueError("Gold row ranges require both bounds")
        if self.row_start is not None and (not self.sheet or self.row_end < self.row_start):
            raise ValueError("Gold rows require a sheet and ordered physical row bounds")
        return self


class WorkbenchCase(AnswerCase):
    model_config = ConfigDict(extra="forbid", frozen=True)
    search_mode: Literal["knowledge"] = "knowledge"
    reference_answer: str = Field(default="", max_length=16000)
    split: Literal["dev", "frozen", "custom"] = "custom"
    gold_anchors: tuple[GoldAnchor, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def job_owns_scope(self):
        # Labels cannot impersonate users or select private documents. The job
        # resolves its authenticated creator's explicit scope before execution.
        if self.user_id is not None or self.document_ids:
            raise ValueError("Evaluation identity and document scope belong to the job")
        if self.web_query is not None or self.web_urls:
            raise ValueError("RAG datasets cannot request external web access")
        return self


class WorkbenchDataset(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    cases: tuple[WorkbenchCase, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_cases(self):
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Evaluation case IDs must be unique")
        return self

    @property
    def fingerprint(self):
        return digest(self.model_dump(mode="json"))
