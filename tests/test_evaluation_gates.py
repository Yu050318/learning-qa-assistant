import pytest
from pydantic import ValidationError

from app.application.evaluation import AnswerCase, Prediction, SemanticReview, evaluate, review_digest
from app.application.evaluation_gates import EvaluationGatePolicy, MetricGate, evaluate_gates


def inputs():
    cases = [AnswerCase(id=key, category="fixture", question="Power?", expected_kinds=["grounded"], expected_facts=["100W"]) for key in ("a", "b")]
    predictions = [Prediction(id=case.id, answer="100W[1]", kind="grounded") for case in cases]
    report = {"execution_failed_cases": 0, "cases": [
        {"id": key, "reviewer": "model", "metrics": {"faithfulness": value}} for key, value in (("a", .8), ("b", 1.))]}
    return report, cases, predictions


def test_absolute_thresholds_require_complete_eligible_metric_coverage():
    report, cases, predictions = inputs()
    policy = EvaluationGatePolicy(metric_gates=[MetricGate(metric="faithfulness", minimum=.9)])
    checks = evaluate_gates(policy, report, cases, predictions)
    assert all(check["passed"] for check in checks)
    assert checks[-1]["value"] == .9 and checks[-1]["scored_cases"] == checks[-1]["eligible_cases"] == 2
    report["cases"][0]["metrics"]["faithfulness"] = None
    checks = evaluate_gates(policy, report, cases, predictions)
    assert not checks[-1]["passed"] and checks[-1]["value"] == 1
    optional = EvaluationGatePolicy(metric_gates=[MetricGate(metric="faithfulness", minimum=.9, required=False)])
    assert evaluate_gates(optional, report, cases, predictions)[-1]["passed"]


def test_required_unknown_metric_and_incomparable_regression_do_not_pass():
    report, cases, predictions = inputs()
    policy = EvaluationGatePolicy(metric_gates=[MetricGate(metric="faithfulness", minimum=.8, max_regression=.01)])
    report["comparison"] = {"metric_deltas": {"faithfulness": -.02}}
    assert not evaluate_gates(policy, report, cases, predictions)[-1]["passed"]
    report["comparison"]["metric_deltas"]["faithfulness"] = -.005
    assert evaluate_gates(policy, report, cases, predictions)[-1]["passed"]
    report["comparison"]["metric_deltas"]["faithfulness"] = None
    assert not evaluate_gates(policy, report, cases, predictions)[-1]["passed"]
    for row in report["cases"]:
        row["metrics"]["faithfulness"] = None
    check = evaluate_gates(policy, report, cases, predictions)[-1]
    assert check["value"] is None and not check["passed"] and check["reason"] == "required_unscored"


def test_no_answer_only_dataset_has_no_fabricated_semantic_scores():
    case = AnswerCase(id="missing", category="no-answer", question="Unknown?", expected_kinds=["insufficient"])
    prediction = Prediction(id=case.id, answer="没有找到资料。", kind="insufficient")
    review = SemanticReview(id=case.id, reviewer="model", input_sha256=review_digest(case, prediction), judge_provider="fixture",
        judge_model="fixture", judge_prompt_sha256="f" * 64, claims=[], fact_coverage=[])
    report = evaluate([case], [prediction], [review])
    report["execution_failed_cases"] = 0
    checks = evaluate_gates(EvaluationGatePolicy(), report, [case], [prediction])
    assert all(check["passed"] for check in checks)
    semantic = next(check for check in checks if check["name"] == "faithfulness")
    assert semantic["value"] is None and semantic["reason"] == "not_applicable" and semantic["eligible_cases"] == 0


@pytest.mark.parametrize("gate", [{"metric": "unknown"}, {"metric": "faithfulness", "minimum": True},
    {"metric": "faithfulness", "minimum": float("nan")}, {"metric": "faithfulness", "minimum": .9, "maximum": .8},
    {"metric": "faithfulness", "max_regression": -1}])
def test_invalid_gate_contracts_are_rejected(gate):
    with pytest.raises(ValidationError):
        MetricGate.model_validate(gate)


def test_duplicate_metric_gates_and_vacuous_policies_are_rejected():
    for gates in ([], [{"metric": "faithfulness"}, {"metric": "faithfulness"}]):
        with pytest.raises(ValidationError):
            EvaluationGatePolicy(metric_gates=gates)
