"""Frozen quality policies; unknown values never satisfy required metric gates."""
import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.entity.evaluation.evaluation import StrictModel

Metric = Literal["kind_correct", "citations_valid", "source_types_correct", "faithfulness", "fact_coverage",
                 "citation_support", "claim_citation_coverage", "citation_validity"]
Score = Annotated[float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)]
DEFAULT_METRICS = ("kind_correct", "citations_valid", "source_types_correct", "faithfulness", "fact_coverage", "citation_support", "claim_citation_coverage")


class MetricGate(StrictModel):
    metric: Metric
    minimum: Score | None = 1.
    maximum: Score | None = None
    direction: Literal["higher", "lower"] = "higher"
    max_regression: Score = 0.
    required: bool = True

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Metric minimum cannot exceed maximum")
        return self


class EvaluationGatePolicy(StrictModel):
    schema_version: Literal[1] = 1
    max_failed_cases: int = Field(default=0, ge=0, le=1000, strict=True)
    require_model_review: bool = True
    require_comparison: bool = False
    metric_gates: tuple[MetricGate, ...] = Field(default_factory=lambda: tuple(MetricGate(metric=name) for name in DEFAULT_METRICS), min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_metrics(self):
        names = [gate.metric for gate in self.metric_gates]
        if len(names) != len(set(names)):
            raise ValueError("Metric gates must be unique")
        return self


def evaluate_gates(policy, report, cases, predictions):
    policy = EvaluationGatePolicy.model_validate(policy)
    rows = {row["id"]: row for row in report["cases"]}
    observed = {prediction.id: prediction for prediction in predictions}
    generated = [case.id for case in cases if case.id in observed and not observed[case.id].error_code]
    reviewed = sum(rows[key].get("reviewer") == "model" for key in generated)
    checks = [{"name": "execution_failures", "passed": report["execution_failed_cases"] <= policy.max_failed_cases,
               "value": report["execution_failed_cases"], "maximum": policy.max_failed_cases},
              {"name": "model_review", "passed": not policy.require_model_review or reviewed == len(generated),
               "scored_cases": reviewed, "eligible_cases": len(generated)},
              {"name": "comparison", "passed": not policy.require_comparison or report.get("comparison") is not None}]

    def eligible(case, name):
        prediction = observed.get(case.id)
        grounded = "grounded" in case.expected_kinds or bool(prediction and prediction.kind == "grounded")
        if name == "fact_coverage":
            return bool(case.expected_facts)
        if name in {"faithfulness", "citation_support", "claim_citation_coverage"}:
            return grounded
        if name == "citation_validity":
            return grounded or bool(prediction and re.search(r"\[\d+\]", prediction.answer))
        return True

    for gate in policy.metric_gates:
        ids = [case.id for case in cases if eligible(case, gate.metric)]
        values = [rows[key]["metrics"].get(gate.metric) for key in ids]
        scored = [value for value in values if value is not None]
        value = round(sum(scored) / len(scored), 6) if scored else None
        delta = (report.get("comparison") or {}).get("metric_deltas", {}).get(gate.metric)
        check = {"name": gate.metric, "value": value, "eligible_cases": len(ids), "scored_cases": len(scored),
                 "minimum": gate.minimum, "maximum": gate.maximum, "max_regression": gate.max_regression, "delta": delta}
        if not ids:
            check.update(passed=True, reason="not_applicable")
        elif value is None:
            check.update(passed=not gate.required, reason="required_unscored" if gate.required else "optional_unscored")
        else:
            coverage = not gate.required or len(scored) == len(ids)
            bounds = (gate.minimum is None or value >= gate.minimum) and (gate.maximum is None or value <= gate.maximum)
            regression = True
            if report.get("comparison") is not None:
                regression = (not gate.required) if delta is None else (delta >= -gate.max_regression if gate.direction == "higher" else delta <= gate.max_regression)
            check.update(passed=coverage and bounds and regression, reason="checked")
        checks.append(check)
    return checks
