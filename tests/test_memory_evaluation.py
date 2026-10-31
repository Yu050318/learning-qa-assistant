import json
from pathlib import Path

import pytest

from app.entity.evaluation.memory import MemoryDataset, prediction_digest
from app.service.memory_evaluation import summarize


def test_frozen_memory_corpus_has_independent_task_families():
    dataset = MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    assert len(dataset.cases) == 120
    assert sum(c.split == 'dev' for c in dataset.cases) == 20
    assert sum(c.split == 'frozen' for c in dataset.cases) == 100
    assert {len(c.sessions) + 1 for c in dataset.cases} <= {2, 3, 4}
    assert {c.family for c in dataset.cases if c.split == 'dev'}.isdisjoint(c.family for c in dataset.cases if c.split == 'frozen')


def test_missing_results_and_reviews_never_manufacture_quality_or_zero_usage():
    dataset = MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    report = summarize(dataset, [])
    assert report['groups']['baseline']['executed'] == 0
    assert report['groups']['baseline']['execution_failures'] == 0
    assert report['groups']['baseline']['missing_predictions'] == 300
    assert report['groups']['baseline']['correctness'] is None
    assert report['groups']['baseline']['answer_usage'] is None
    assert report['accuracy_delta_pp'] is None and not report['quality_gate_passed']


def test_literal_matches_do_not_count_as_semantic_reviews_and_duplicates_are_rejected():
    dataset = MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    row = {'group': 'baseline', 'case_id': dataset.cases[20].id, 'trial': 1,
           'answer': '厦门', 'literal_fact_match': True, 'provider': 'fixture', 'model': 'fixture', 'error_code': None}
    report = summarize(dataset, [row])
    assert report['groups']['baseline']['reviewed'] == 0 and report['groups']['baseline']['correctness'] is None
    with pytest.raises(ValueError, match='Duplicate'):
        summarize(dataset, [row, row])


def test_reviews_are_bound_to_exact_predictions_and_partial_rates_stay_unavailable():
    dataset = MemoryDataset.model_validate_json(Path('evals/memory/personal-v1.json').read_text(encoding='utf-8'))
    case = next(c for c in dataset.cases if c.split == 'frozen' and c.category == 'corrections')
    row = {'group': 'openviking', 'case_id': case.id, 'trial': 1, 'answer': 'fixture', 'error_code': None}
    review = {'group': 'openviking', 'case_id': case.id, 'trial': 1, 'reviewer': 'human', 'correct': True,
              'unsupported_personal_fact': False, 'used_updated_fact': True, 'prediction_sha256': prediction_digest(row)}
    report = summarize(dataset, [row], [review])
    group = report['groups']['openviking']
    assert group['updated_fact_denominator'] == 60 and group['updated_fact_reviewed'] == 1
    assert group['updated_fact_rate'] is None and group['unsupported_personal_fact_rate'] is None
    assert not report['isolation_gate_passed']
    with pytest.raises(ValueError, match='digest'):
        summarize(dataset, [{**row, 'answer': 'changed'}], [review])


def test_controlled_retrieval_failures_are_kept_in_denominator():
    from scripts.evaluate_memory_retrieval import summarize_trials
    report = summarize_trials([
        {'case_id': 'a', 'trial': 1, 'error_code': None, 'baseline_recall_at_3': 1, 'openviking_recall_at_3': 1},
        {'case_id': 'a', 'trial': 2, 'error_code': 'INDEX_FAILED'},
    ])
    assert report['execution_failures'] == 1 and report['executed'] == 2
    assert report['openviking_recall_at_3'] == .5


def test_live_memory_request_matches_existing_http_contract():
    import httpx
    from app.entity.vo.schemas import V2ChatRequest
    from app.manager.memory_evaluation import MemoryEvaluationClient
    def handle(request):
        payload = V2ChatRequest.model_validate_json(request.content)
        assert payload.document_ids is None and payload.search_mode == 'knowledge'
        return httpx.Response(200, json={'answer': 'fixture'})
    client = MemoryEvaluationClient('http://test', 'fixture-token', transport=httpx.MockTransport(handle))
    try:
        assert client.ask('fixture-session', '我的偏好是什么？')['answer'] == 'fixture'
    finally:
        client.close()


def test_memory_recency_is_context_metadata_and_does_not_change_citation_contract():
    from uuid import uuid4
    from app.manager.evidence import EvidenceRegistry
    from app.entity.vo.schemas import MemoryCitation
    registry = EvidenceRegistry()
    result = registry.add_memories([{'memory_id': uuid4(), 'content': '我改用英文。',
        'score': .8, 'asserted_at': '2026-10-29T00:01:00+00:00'}])
    assert result['sources'][0]['asserted_at'] == '2026-10-29T00:01:00+00:00'
    MemoryCitation.model_validate(registry.entries[0])


def test_evaluation_client_identifies_balance_exhaustion_without_raw_body():
    import httpx
    from app.manager.memory_evaluation import MemoryEvaluationClient
    client = MemoryEvaluationClient('http://test', 'fixture', transport=httpx.MockTransport(
        lambda request: httpx.Response(503, json={'error': {'code': 'MODEL_BALANCE_EXHAUSTED', 'message': 'private upstream details'}})))
    try:
        with pytest.raises(RuntimeError, match='^MODEL_BALANCE_EXHAUSTED$'):
            client.identity()
    finally:
        client.close()
