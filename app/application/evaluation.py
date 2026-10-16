"""Answer evaluation with explicit semantic reviews; no model calls in the scorer."""

import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, TypeAdapter, model_validator

from app.api.schemas import V2Citation


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnswerCase(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=8000)
    search_mode: Literal["knowledge", "sql", "web", "auto"] = "knowledge"
    expected_kinds: list[Literal["grounded", "clarification", "insufficient", "smalltalk"]] = Field(min_length=1)
    expected_source_types: list[Literal["knowledge", "sql", "web", "memory"]] = Field(default_factory=list)
    expected_facts: list[str] = Field(default_factory=list, max_length=50)
    user_id: UUID | None = None
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    web_query: str | None = None
    web_urls: list[str] = Field(default_factory=list, max_length=3)


class EvidenceRecord(StrictModel):
    citation: dict
    content: str = Field(min_length=1, max_length=12000)


class Prediction(StrictModel):
    id: str
    answer: str = Field(default="", max_length=16000)
    kind: Literal["grounded", "clarification", "insufficient", "smalltalk"] | None = None
    citations: list[dict] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceRecord] = Field(default_factory=list, max_length=100)
    error_code: str | None = None
    run_metadata: dict = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_snapshot(self):
        citations = [citation_adapter.validate_python(item.citation) for item in self.evidence]
        numbers = [item.number for item in citations]
        if any(number <= 0 for number in numbers) or len(set(numbers)) != len(numbers):
            raise ValueError("Invalid evidence numbers")
        for item, citation in zip(self.evidence, citations):
            item.citation = citation.model_dump(mode="json")
        if not self.error_code and (not self.answer.strip() or self.kind is None):
            raise ValueError("Successful predictions require answer and kind")
        return self


class Quote(StrictModel):
    number: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)


class ClaimReview(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    verdict: Literal["supported", "unsupported", "contradicted", "unverifiable"]
    quotes: list[Quote] = Field(default_factory=list, max_length=20)


class SemanticReview(StrictModel):
    id: str
    reviewer: Literal["human", "model"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    judge_provider: str | None = None
    judge_model: str | None = None
    judge_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    claims: list[ClaimReview] = Field(max_length=100)
    fact_coverage: list[Literal["covered", "missing", "incorrect"]] = Field(max_length=50)


citation_adapter = TypeAdapter(V2Citation)
CONFIG_KEYS = ("retrieval_top_k", "retrieval_parent_context", "retrieval_parent_group_size", "rerank_enabled",
               "rerank_model", "knowledge_quality_enabled", "embedding_model", "agent_max_model_calls",
               "agent_max_tool_calls", "agent_max_knowledge_calls", "document_versions_enabled")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def review_digest(case: AnswerCase, prediction: Prediction) -> str:
    return digest({"case": case.model_dump(mode="json"), "answer": prediction.answer, "kind": prediction.kind,
                   "citations": prediction.citations, "evidence": [item.model_dump(mode="json") for item in prediction.evidence]})


def _ratio(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def validate_review(case: AnswerCase, prediction: Prediction, review: SemanticReview) -> None:
    if review.id != case.id or review.input_sha256 != review_digest(case, prediction) or len(review.fact_coverage) != len(case.expected_facts):
        raise ValueError("Review does not cover the labelled facts")
    if review.reviewer == "model" and not (review.judge_provider and review.judge_model and review.judge_prompt_sha256):
        raise ValueError("Model reviews require actual judge provenance")
    if prediction.kind == "grounded" and not review.claims:
        raise ValueError("Grounded answers require explicit factual claim review")
    sources = {item.citation["number"]: item.content for item in prediction.evidence}
    spans = []
    for claim in review.claims:
        if prediction.answer.count(claim.text) != 1:
            raise ValueError("Reviewed claims must be unique verbatim answer spans")
        start = prediction.answer.index(claim.text)
        spans.append((start, start + len(claim.text)))
        if claim.verdict in {"supported", "contradicted"} and not claim.quotes:
            raise ValueError("Semantic verdict requires verifiable evidence quotes")
        for quote in claim.quotes:
            if quote.number not in sources or quote.text not in sources[quote.number]:
                raise ValueError("Review quote is not in this run's eligible leaf evidence")
    spans.sort()
    if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
        raise ValueError("Claim spans must not overlap")


def score_case(case: AnswerCase, prediction: Prediction | None, review: SemanticReview | None = None) -> dict:
    empty = {"faithfulness": None, "citation_support": None, "fact_coverage": None, "claim_citation_coverage": None}
    if prediction is None or prediction.error_code:
        return {"id": case.id, "category": case.category, "status": "failed",
                "error_code": prediction.error_code if prediction else "MISSING_PREDICTION",
                "metrics": {"kind_correct": 0., "citations_valid": 0., "source_types_correct": 0., "citation_validity": None, **empty}}
    refs = {int(number) for number in re.findall(r"\[(\d+)\]", prediction.answer)}
    observed = {item.citation["number"]: item.citation for item in prediction.evidence}
    provided, invalid_cards = {}, 0
    for card in prediction.citations:
        try:
            if type(card.get("number")) is not int:
                raise ValueError("Citation number must be an integer")
            parsed = citation_adapter.validate_python(card).model_dump(mode="json")
            number = parsed["number"]
            expected = citation_adapter.validate_python(observed[number]).model_dump(mode="json")
            if number in provided or parsed != expected:
                raise ValueError("Mismatched or duplicate provenance")
            provided[number] = parsed
        except (ValueError, KeyError, TypeError):
            invalid_cards += 1
    valid = refs & provided.keys()
    source_types = {provided[number]["type"] for number in valid}
    allowed = {"knowledge": "knowledge", "sql": "sql", "web": "web"}.get(case.search_mode)
    scope_ok = not allowed or source_types.issubset({allowed, "memory"})
    if case.document_ids:
        allowed_documents = {str(document_id) for document_id in case.document_ids}
        scope_ok = scope_ok and all(card["type"] != "knowledge" or card["document_id"] in allowed_documents for number, card in provided.items() if number in valid)
    kind_ok = prediction.kind in case.expected_kinds
    cards_ok = not invalid_cards and set(provided) == refs
    citations_ok = cards_ok and valid == refs and scope_ok and (prediction.kind != "grounded" or bool(refs))
    metrics = {"kind_correct": float(kind_ok), "citations_valid": float(citations_ok),
               "citation_validity": _ratio(len(valid), len(refs)),
               "source_types_correct": float(source_types == set(case.expected_source_types)), **empty}
    review_error = None
    if review is not None:
        try:
            validate_review(case, prediction, review)
            supported, cited_claims, supported_citations = 0, 0, 0
            for claim in review.claims:
                numbers = {int(value) for value in re.findall(r"\[(\d+)\]", claim.text)}
                supported += claim.verdict == "supported"
                cited_claims += bool(numbers)
                supporting = {quote.number for quote in claim.quotes}
                supported_citations += bool(numbers) and claim.verdict == "supported" and numbers.issubset(valid & supporting)
            metrics.update(faithfulness=_ratio(supported, len(review.claims)),
                           citation_support=_ratio(supported_citations, cited_claims),
                           claim_citation_coverage=_ratio(cited_claims, len(review.claims)),
                           fact_coverage=_ratio(review.fact_coverage.count("covered"), len(case.expected_facts)))
        except ValueError:
            review_error = "INVALID_SEMANTIC_REVIEW"
    trace = prediction.run_metadata
    return {"id": case.id, "category": case.category, "status": "ok", "metrics": metrics,
            "reviewer": review.reviewer if review and not review_error else None, "review_error": review_error,
            "judge_policy": {key: getattr(review, key) for key in ("judge_provider", "judge_model", "judge_prompt_sha256")} if review and not review_error and review.reviewer == "model" else None,
            "reviewed_claims": len(review.claims) if review and not review_error else 0,
            "unknown_reference_numbers": sorted(refs - valid), "invalid_citation_cards": invalid_cards,
            "model": prediction.model, "provider": prediction.provider,
            "trace": {key: trace.get(key) for key in ("elapsed_ms", "model_attempts", "knowledge_searches", "knowledge_rewrites", "warnings")}}


def _unique(items):
    result = {item.id: item for item in items}
    if len(result) != len(items):
        raise ValueError("Duplicate evaluation IDs")
    return result


def evaluate(cases: list[AnswerCase], predictions: list[Prediction], reviews: list[SemanticReview] = ()) -> dict:
    if not cases:
        raise ValueError("Empty evaluation dataset")
    case_map, prediction_map, review_map = _unique(cases), _unique(predictions), _unique(reviews)
    if set(prediction_map) - set(case_map) or set(review_map) - set(case_map):
        raise ValueError("Unexpected case IDs")
    details = [score_case(case, prediction_map.get(case.id), review_map.get(case.id)) for case in cases]

    def summary(rows):
        names = {name for row in rows for name in row["metrics"]}
        aggregates = {}
        for name in sorted(names):
            values = [row["metrics"].get(name) for row in rows if row["metrics"].get(name) is not None]
            aggregates[name] = {"value": _ratio(sum(values), len(values)), "scored_cases": len(values)}
        return {"cases": len(rows), "successful_runs": sum(row["status"] == "ok" for row in rows),
                "failed_runs": sum(row["status"] == "failed" for row in rows),
                "invalid_reviews": sum(bool(row.get("review_error")) for row in rows), "metrics": aggregates}

    return {"schema_version": 1, "dataset_sha256": digest([case.model_dump(mode="json") for case in cases]),
            "predictions_sha256": digest([prediction.model_dump(mode="json") for prediction in predictions]),
            "reviews_sha256": digest([review.model_dump(mode="json") for review in reviews]),
            "summary": summary(details),
            "by_category": {category: summary([row for row in details if row["category"] == category]) for category in sorted({case.category for case in cases})},
            "cases": details}


def compare_reports(current: dict, baseline: dict) -> dict:
    if current["dataset_sha256"] != baseline["dataset_sha256"]:
        raise ValueError("Comparisons require the same labelled dataset")
    def policy(report):
        return {row["id"]: (row.get("reviewer"), row.get("judge_policy")) for row in report["cases"]}
    changed_policy = policy(current) != policy(baseline)
    deltas = {}
    semantic = {"faithfulness", "citation_support", "claim_citation_coverage", "fact_coverage"}
    for name, metric in current["summary"]["metrics"].items():
        other = baseline["summary"]["metrics"].get(name, {})
        cohort = lambda report: {row["id"] for row in report["cases"] if row["metrics"].get(name) is not None}
        comparable = (metric["value"] is not None and other.get("value") is not None
                      and cohort(current) == cohort(baseline) and not (changed_policy and name in semantic))
        deltas[name] = round(metric["value"] - other["value"], 6) if comparable else None
    return {"baseline_predictions_sha256": baseline["predictions_sha256"], "review_policy_changed": changed_policy,
            "metric_deltas": deltas}
