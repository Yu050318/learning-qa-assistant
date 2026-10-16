from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from app.application.evaluation import (AnswerCase, Prediction, SemanticReview, compare_reports, evaluate,
                                        review_digest, score_case)
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, SearchHit
from app.workflows.supervisor import SupervisorWorkflow
from scripts.evaluate_answers import judge_prediction, load_items, predict_live
from test_agent_v2 import Models, ScriptedModel, Web, call, final, report


ROOT = Path(__file__).parent / "fixtures" / "answer-evaluation"


@pytest.fixture
def labelled():
    return (load_items(ROOT / "cases.json", AnswerCase), load_items(ROOT / "predictions.json", Prediction),
            load_items(ROOT / "reviews.json", SemanticReview))


def test_synthetic_controls_distinguish_semantics_from_citation_legality(labelled):
    cases, predictions, reviews = labelled
    results = evaluate(cases, predictions, reviews)
    rows = {row["id"]: row for row in results["cases"]}
    assert results["summary"]["invalid_reviews"] == 0
    assert rows["excel-good"]["metrics"]["faithfulness"] == 1
    assert rows["excel-wrong-row"]["metrics"]["faithfulness"] == 1
    assert rows["excel-wrong-row"]["metrics"]["citations_valid"] == 0
    assert rows["excel-wrong-row"]["metrics"]["citation_support"] == 0
    assert rows["hallucinated-warranty"]["metrics"]["faithfulness"] == .5
    assert rows["hallucinated-warranty"]["metrics"]["claim_citation_coverage"] == .5
    assert rows["hallucinated-warranty"]["metrics"]["citation_support"] == 0
    assert rows["parent-not-evidence"]["metrics"]["citations_valid"] == 1
    assert rows["parent-not-evidence"]["metrics"]["faithfulness"] == 0
    assert rows["unknown-reference"]["unknown_reference_numbers"] == [99]
    assert rows["mixed-sql-web"]["metrics"]["fact_coverage"] == 1
    assert rows["unreviewed"]["metrics"]["faithfulness"] is None
    assert rows["smalltalk"]["metrics"]["faithfulness"] is None


def test_absent_reviews_never_mean_semantic_success(labelled):
    cases, predictions, _ = labelled
    result = evaluate(cases, predictions)
    for name in ("faithfulness", "citation_support", "fact_coverage"):
        assert result["summary"]["metrics"][name] == {"value": None, "scored_cases": 0}


def test_failed_and_missing_runs_are_counted_without_fake_semantic_scores(labelled):
    cases, _, _ = labelled
    results = evaluate(cases[:2], [Prediction(id=cases[0].id, error_code="MILVUS_UNAVAILABLE")])
    assert results["summary"]["failed_runs"] == 2
    assert results["summary"]["metrics"]["kind_correct"] == {"value": 0., "scored_cases": 2}
    assert results["summary"]["metrics"]["faithfulness"]["value"] is None
    assert results["cases"][1]["error_code"] == "MISSING_PREDICTION"


@pytest.mark.parametrize("mutation", ["invented-quote", "unknown-number", "duplicate-claim", "missing-fact", "stale-hash", "not-in-answer", "no-claim"])
def test_invalid_semantic_labels_are_unscored_and_visible(labelled, mutation):
    cases, predictions, reviews = labelled
    case, prediction, review = cases[0], predictions[0], reviews[0].model_copy(deep=True)
    if mutation == "invented-quote":
        review.claims[0].quotes[0].text = "保修十年"
    elif mutation == "unknown-number":
        review.claims[0].quotes[0].number = 99
    elif mutation == "duplicate-claim":
        review.claims.append(review.claims[0].model_copy(deep=True))
    elif mutation == "missing-fact":
        review.fact_coverage = []
    elif mutation == "stale-hash":
        review.input_sha256 = "0" * 64
    elif mutation == "not-in-answer":
        review.claims[0].text = "答案中不存在的句子"
    else:
        review.claims = []
    result = score_case(case, prediction, review)
    assert result["review_error"] == "INVALID_SEMANTIC_REVIEW"
    assert result["metrics"]["faithfulness"] is None


def test_stale_review_cannot_be_reused_for_changed_reference_facts(labelled):
    cases, predictions, reviews = labelled
    case = cases[0].model_copy(update={"expected_facts": ["NX-1 功率为999W"]})
    assert score_case(case, predictions[0], reviews[0])["review_error"] == "INVALID_SEMANTIC_REVIEW"


@pytest.mark.parametrize("mutation", ["extra-card", "duplicate-card", "wrong-document", "malformed-card", "wrong-mode"])
def test_citation_cards_must_match_this_run_snapshot(labelled, mutation):
    cases, predictions, _ = labelled
    case, prediction = cases[0], predictions[0].model_copy(deep=True)
    if mutation == "extra-card":
        prediction.citations.append({**prediction.citations[0], "number": 2})
    elif mutation == "duplicate-card":
        prediction.citations.append(deepcopy(prediction.citations[0]))
    elif mutation == "wrong-document":
        prediction.citations[0]["document_id"] = str(uuid4())
    elif mutation == "malformed-card":
        prediction.citations[0] = {"number": 1, "type": "context_only"}
    else:
        case = case.model_copy(update={"search_mode": "web"})
    assert score_case(case, prediction)["metrics"]["citations_valid"] == 0


def test_duplicate_and_unknown_ids_are_rejected_before_scoring(labelled):
    cases, predictions, reviews = labelled
    with pytest.raises(ValueError):
        evaluate([cases[0], cases[0]], [])
    with pytest.raises(ValueError):
        evaluate(cases[:1], predictions[:2])
    with pytest.raises(ValueError):
        evaluate(cases, predictions, [reviews[0], reviews[0]])
    with pytest.raises(ValueError):
        evaluate([], [])


def test_snapshot_numbers_are_unique_and_parent_context_is_not_a_citation(labelled):
    _, predictions, _ = labelled
    value = predictions[0].model_dump(mode="json")
    value["evidence"].append(deepcopy(value["evidence"][0]))
    with pytest.raises(ValueError):
        Prediction.model_validate(value)
    value["evidence"] = [{"citation": {"type": "context_only", "number": 1}, "content": "保修十年"}]
    with pytest.raises(ValueError):
        Prediction.model_validate(value)


def test_baseline_requires_same_dataset_and_reviewed_cohort(labelled):
    cases, predictions, reviews = labelled
    baseline = evaluate(cases, predictions, reviews)
    assert all(value in (None, 0) for value in compare_reports(baseline, baseline)["metric_deltas"].values())
    with pytest.raises(ValueError):
        compare_reports(evaluate(cases[:-1], predictions[:-1], reviews), baseline)
    current = evaluate(cases, predictions, reviews[:-1])
    comparison = compare_reports(current, baseline)
    assert comparison["review_policy_changed"]
    assert comparison["metric_deltas"]["faithfulness"] is None


def test_offline_scope_checks_the_explicit_document_whitelist(labelled):
    cases, predictions, _ = labelled
    case = cases[0].model_copy(update={"document_ids": [uuid4()]})
    assert score_case(case, predictions[0])["metrics"]["citations_valid"] == 0


def test_judge_reuses_model_runtime_once_and_disallows_tool_actions(labelled):
    cases, predictions, reviews = labelled
    response = reviews[0].model_copy(update={"reviewer": "model"})
    model = ScriptedModel(responses=[AIMessage(content=response.model_dump_json())])
    review, trace = judge_prediction(cases[0], predictions[0], Settings(_env_file=None), Models(model), "deepseek")
    assert review.reviewer == "model" and trace["model_attempts"] == 1
    assert review.input_sha256 == review_digest(cases[0], predictions[0])
    assert review.judge_provider == "deepseek" and review.judge_model == "scripted"
    assert "不使用自身知识" in model.seen_calls[0][0].content
    bad = ScriptedModel(responses=[call("execute_sql", query="DROP TABLE sales.orders")])
    with pytest.raises(ValueError):
        judge_prediction(cases[0], predictions[0], Settings(_env_file=None), Models(bad), "deepseek")
    assert bad.position == 1


def test_different_recorded_judge_models_are_not_a_semantic_ab_comparison(labelled):
    cases, predictions, reviews = labelled
    first_review = reviews[0].model_copy(update={"reviewer": "model", "judge_provider": "deepseek", "judge_model": "model-a", "judge_prompt_sha256": "1" * 64})
    second_review = first_review.model_copy(update={"judge_model": "model-b"})
    baseline = evaluate(cases[:1], predictions[:1], [first_review])
    current = evaluate(cases[:1], predictions[:1], [second_review])
    comparison = compare_reports(current, baseline)
    assert comparison["review_policy_changed"]
    assert comparison["metric_deltas"]["faithfulness"] is None
    assert comparison["metric_deltas"]["citations_valid"] == 0


def test_judge_context_limit_blocks_model_call(labelled):
    cases, predictions, _ = labelled
    prediction = predictions[0].model_copy(deep=True)
    prediction.evidence[0].content = "长" * 12000
    model = ScriptedModel(responses=[])
    with pytest.raises(AppError) as error:
        judge_prediction(cases[0], prediction, Settings(_env_file=None, agent_context_token_budget=4000), Models(model), "deepseek")
    assert error.value.code == "CONTEXT_LIMIT_EXCEEDED" and model.position == 0


def test_live_runner_checks_explicit_scope_and_captures_real_supervisor_evidence(labelled):
    cases, _, _ = labelled
    uid, did = uuid4(), uuid4()
    case = cases[0].model_copy(update={"user_id": uid, "document_ids": [did]})
    model = ScriptedModel(responses=[call("delegate_rag", task="查询功率"), call("search_knowledge", query="NX-1"), report(), final("功率100W[1]")])
    retriever = SimpleNamespace(retrieve=lambda *args, **kwargs: [SearchHit(Chunk(str(uuid4()), str(uid), str(did), 0, "NX-1 功率100W", "manual.md"), .03)])
    database = MagicMock()
    services = SimpleNamespace(settings=Settings(_env_file=None), models=Models(model), retriever=retriever, database=database,
                               web_search=Web(), web_reader=None, sql=None)
    with patch("app.infrastructure.persistence.repository.Repository.ready_document_ids", return_value=[did]) as authorized:
        result = predict_live(case, services)
    authorized.assert_called_once_with(uid, [did], "text-embedding-v4")
    assert result.error_code is None
    assert result.evidence[0].content == "NX-1 功率100W"
    assert result.citations[0]["chunk_id"] == result.evidence[0].citation["chunk_id"]
    assert result.config["knowledge_quality_enabled"] is False
    assert "database_url" not in result.config
    assert database.sessions.return_value.__enter__.return_value.add.call_count == 0
    assert predict_live(cases[0], services).error_code == "EVAL_EXPLICIT_SCOPE_REQUIRED"


def test_callback_never_exposes_unapproved_candidate_or_unmatched_parent():
    from test_knowledge_quality import plan, grade
    uid, did = uuid4(), uuid4()
    retriever = SimpleNamespace(retrieve=lambda *args, **kwargs: [SearchHit(Chunk(str(uuid4()), str(uid), str(did), 0, "PRIVATE_UNAPPROVED", "manual.md"), .03)])
    model = ScriptedModel(responses=[call("delegate_rag", task="保修条款"), plan(), grade("no_knowledge", summary="未找到保修政策。"), final("保修资料不足。", "insufficient")])
    workflow = SupervisorWorkflow(Settings(_env_file=None, knowledge_quality_enabled=True), retriever, Models(model), Web())
    from app.domain.contracts import RunContext
    context = RunContext("eval", uid, uuid4(), "保修条款？", "knowledge", (did,), "deepseek", None, False)
    captured, events = [], []
    result = workflow.run(context, [], "", lambda event, data: events.append((event, data)), on_evidence=captured.extend)
    assert captured == [] and result.citations == []
    assert "PRIVATE_UNAPPROVED" not in str(events)


def test_offline_cli_never_initializes_services_and_emits_reproducible_report(tmp_path, monkeypatch):
    from scripts import evaluate_answers
    target = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["evaluate_answers", str(ROOT / "cases.json"), "--predictions", str(ROOT / "predictions.json"),
                                     "--reviews", str(ROOT / "reviews.json"), "--output", str(target)])
    with patch("app.application.container.Services", side_effect=AssertionError("offline touched services")):
        assert evaluate_answers.main() == 0
    report_value = json.loads(target.read_text(encoding="utf-8"))
    assert report_value["run_mode"] == "offline_recorded"
    assert report_value["summary"]["cases"] == 10
    assert report_value["judge_events"] == []
    assert "额定功率 100W" not in target.read_text(encoding="utf-8")


def test_cli_does_not_overwrite_input_files(monkeypatch):
    from scripts import evaluate_answers
    monkeypatch.setattr(sys, "argv", ["evaluate_answers", str(ROOT / "cases.json"), "--predictions", str(ROOT / "predictions.json"),
                                     "--output", str(ROOT / "cases.json")])
    with pytest.raises(SystemExit) as error:
        evaluate_answers.main()
    assert error.value.code == 2
