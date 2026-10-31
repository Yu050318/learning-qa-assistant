"""Paired memory experiments, explicit review coverage and scenario-level intervals."""
import math
import random
from collections import defaultdict
from statistics import mean
from time import perf_counter

from app.entity.evaluation.memory import MemoryReview, prediction_digest


def run_scenario(client, case, group, trial):
    row = {'case_id': case.id, 'group': group, 'trial': trial, 'category': case.category,
           'answer': '', 'error_code': None, 'answer_usage': None,
           'extraction_usage': None, 'embedding_usage': None, 'maintenance_usage': None, 'review_usage': None}
    try:
        client.reset(group == 'openviking')
        for content in case.initial_memories:
            client.remember(content)
        preparation = perf_counter()
        for messages in case.sessions:
            session_id = client.session()
            for message in messages:
                client.ask(session_id, message)
                if group == 'openviking':
                    client.settle()
        row['preparation_ms'] = round((perf_counter() - preparation) * 1000, 2)
        if case.forget_before_query:
            client.reset(group == 'openviking')
        session_id, started = client.session(), perf_counter()
        response = client.ask(session_id, case.question)
        row['elapsed_ms'] = round((perf_counter() - started) * 1000, 2)
        row.update(answer=response['answer'], response=response, model=response['model_name'], provider=response['model_provider'])
        usage = response['run_metadata'].get('usage_by_model', {})
        if usage and all(item.get('complete') and item.get('total_tokens') is not None for item in usage.values()):
            row['answer_usage'] = {key: sum(item[key] for item in usage.values()) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
        # Text matching is diagnostic; it is never promoted to semantic correctness.
        row['literal_fact_match'] = all(fact in row['answer'] for fact in case.expected_facts)
        row['literal_forbidden_match'] = any(fact in row['answer'] for fact in case.forbidden_facts)
    except Exception as error:
        # Never include raw HTTP exceptions, URLs, headers, credentials or response bodies.
        row['error_code'] = str(error) if type(error) is RuntimeError and str(error).isupper() else type(error).__name__
    return row


def interval(values, seed=7):
    if not values:
        return None
    rng = random.Random(seed)
    resampled = sorted(mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return [round(resampled[49], 6), round(resampled[1949], 6)]


def percentile(values, quantile):
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def summarize(dataset, predictions, reviews=(), *, repeats=3, split='frozen', isolation=None):
    cases = [case for case in dataset.cases if case.split == split]
    labels = {case.id: case for case in cases}
    observed = {}
    for prediction in predictions:
        key = (prediction['group'], prediction['case_id'], prediction['trial'])
        if key in observed or key[0] not in {'baseline', 'openviking'} or key[1] not in labels or not 1 <= key[2] <= repeats:
            raise ValueError('Duplicate or out-of-scope prediction')
        observed[key] = prediction
    semantic = {}
    for value in reviews:
        review = MemoryReview.model_validate(value)
        key = (review.group, review.case_id, review.trial)
        if key not in observed or key in semantic:
            raise ValueError('Review does not match one unique prediction')
        if review.prediction_sha256 != prediction_digest(observed[key]):
            raise ValueError('Review digest does not match the saved prediction')
        semantic[key] = review
    report = {'schema_version': 1, 'dataset_fingerprint': dataset.fingerprint, 'split': split,
              'case_count': len(cases), 'repeats': repeats, 'groups': {}, 'quality_gate_passed': False,
              'measurement': 'live_predictions_with_explicit_semantic_reviews', 'missing_usage_is_zero': False}
    all_values = {}
    for group in ('baseline', 'openviking'):
        correct, unsupported, updates, forgetting, per_case, elapsed = [], [], [], [], defaultdict(list), []
        executed, failed, reviewed, missing, answer_usage = 0, 0, 0, 0, []
        for case in cases:
            for trial in range(1, repeats + 1):
                key = (group, case.id, trial)
                row, review = observed.get(key), semantic.get(key)
                if row:
                    executed += 1
                if row is None:
                    missing += 1; value = None
                elif row.get('error_code'):
                    failed += 1; value = 0.
                elif review:
                    reviewed += 1; value = float(review.correct)
                else:
                    value = None
                # Every safety rate records its complete required denominator.
                # Missing/failed answers cannot silently shrink review coverage.
                safety = float(review.unsupported_personal_fact) if review and row and not row.get('error_code') else None
                unsupported.append(safety)
                if case.category == 'corrections':
                    updates.append(float(review.used_updated_fact) if review and review.used_updated_fact is not None
                                   and row and not row.get('error_code') else None)
                if case.category == 'forgetting':
                    forgetting.append(safety)
                correct.append(value); per_case[case.id].append(value)
                if row and row.get('elapsed_ms') is not None:
                    elapsed.append(row['elapsed_ms'])
                answer_usage.append(row.get('answer_usage') if row else None)
        complete = bool(correct) and all(v is not None for v in correct)
        per_case_values = {key: mean(values) if all(v is not None for v in values) else None for key, values in per_case.items()}
        all_values[group] = per_case_values
        report['groups'][group] = {'executed': executed, 'missing_predictions': missing, 'execution_failures': failed,
            'reviewed': reviewed, 'review_required': executed - failed, 'correctness': mean(correct) if complete else None,
            'correctness_ci95': interval(list(per_case_values.values())) if complete else None,
            'updated_fact_rate': mean(updates) if updates and all(v is not None for v in updates) else None,
            'updated_fact_denominator': len(updates), 'updated_fact_reviewed': sum(v is not None for v in updates),
            'unsupported_personal_fact_rate': mean(unsupported) if unsupported and all(v is not None for v in unsupported) else None,
            'unsupported_fact_denominator': len(unsupported), 'unsupported_fact_reviewed': sum(v is not None for v in unsupported),
            'forgetting_leaks': sum(forgetting) if forgetting and all(v is not None for v in forgetting) else None,
            'forgetting_denominator': len(forgetting), 'forgetting_reviewed': sum(v is not None for v in forgetting),
            'p95_ms': percentile(elapsed, .95) if len(elapsed) == len(correct) else None,
            'answer_usage': {key: sum(item[key] for item in answer_usage) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
                            if answer_usage and all(item is not None for item in answer_usage) else None,
            'extraction_usage': None, 'embedding_usage': None, 'maintenance_usage': None, 'review_usage': None}
    a, b = report['groups']['baseline'], report['groups']['openviking']
    complete = a['correctness'] is not None and b['correctness'] is not None
    models = {group: {(row.get('provider'), row.get('model')) for row in predictions if row['group'] == group and not row.get('error_code')} for group in ('baseline', 'openviking')}
    comparable = complete and len(models['baseline']) == 1 and models['baseline'] == models['openviking']
    report['comparable'] = comparable
    report['accuracy_delta_pp'] = round((b['correctness'] - a['correctness']) * 100, 4) if comparable else None
    report['paired_delta_ci95_pp'] = [v * 100 for v in interval([all_values['openviking'][c.id] - all_values['baseline'][c.id] for c in cases])] if comparable else None
    expected = {case.id for case in cases}
    isolation_rows = isolation.get('cases', []) if isinstance(isolation, dict) else []
    isolated = (isinstance(isolation, dict) and isolation.get('dataset_fingerprint') == dataset.fingerprint
        and len(isolation_rows) == len(expected) and {r.get('case_id') for r in isolation_rows} == expected
        and all(r.get('source_verified') is True and r.get('cross_user_leak') is False
                and r.get('deleted_recall') is False and r.get('error_code') is None for r in isolation_rows))
    report['isolation_gate_passed'] = bool(isolated)
    report['isolation'] = isolation if isinstance(isolation, dict) else None
    report['quality_gate_passed'] = bool(comparable and a['execution_failures'] == b['execution_failures'] == 0
        and b['correctness'] >= .85 and report['accuracy_delta_pp'] >= 10
        and b['updated_fact_rate'] is not None and b['updated_fact_rate'] >= .9
        and b['unsupported_personal_fact_rate'] is not None and b['unsupported_personal_fact_rate'] <= .05
        and b['forgetting_leaks'] == 0 and isolated
        and a['p95_ms'] and b['p95_ms'] is not None and b['p95_ms'] <= a['p95_ms'] * 1.1)
    return report


def markdown(report):
    lines = ['# Personal memory paired evaluation', '',
             'Quality gate: ' + ('PASS' if report['quality_gate_passed'] else 'NOT PASSED / INCOMPLETE'), '',
             'Dataset fingerprint: `' + report['dataset_fingerprint'] + '`', '',
             '| Metric | Baseline | OpenViking |', '| --- | --- | --- |']
    for name in ('executed', 'missing_predictions', 'execution_failures', 'reviewed', 'correctness', 'updated_fact_rate',
                 'updated_fact_denominator', 'updated_fact_reviewed', 'unsupported_personal_fact_rate',
                 'unsupported_fact_denominator', 'unsupported_fact_reviewed', 'forgetting_leaks',
                 'forgetting_denominator', 'forgetting_reviewed', 'p95_ms', 'answer_usage',
                 'extraction_usage', 'embedding_usage', 'maintenance_usage', 'review_usage'):
        values = [report['groups'][group][name] for group in ('baseline', 'openviking')]
        lines.append('| ' + name + ' | ' + ' | '.join('unavailable' if v is None else str(v) for v in values) + ' |')
    lines += ['', 'Accuracy delta (percentage points): ' + str(report['accuracy_delta_pp']), '',
              'Cross-user and deletion gate: ' + ('PASS' if report['isolation_gate_passed'] else 'NOT PASSED / INCOMPLETE'), '',
              'Unavailable model usage is not zero. Literal matching is diagnostic only. Failures stay in the denominator.',
              'Intervals resample independent scenarios, not repeated answers. Missing semantic reviews do not satisfy a quality gate.']
    return '\n'.join(lines) + '\n'
