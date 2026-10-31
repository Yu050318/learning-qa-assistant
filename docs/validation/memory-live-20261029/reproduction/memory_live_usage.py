"""Read-only phase accounting: reported quantities are never promoted to total bills."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from math import isfinite
from statistics import mean
from uuid import UUID

from sqlalchemy import select
from app.dao.postgres.database import Database
from app.entity.do.models import ChatMessage, MemorySyncJob, ModelCall
from app.entity.time import aware
from app.manager.openviking import OpenVikingManager
from app.service.memory_evaluation import percentile
from tmp.memory_live_runtime import settings, LOCAL


def read(name):
    return json.loads((LOCAL / name).read_text(encoding='utf-8'))


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0


def totals(values, fields):
    reported = [v for v in values if isinstance(v, dict) and all(numeric(v.get(k)) for k in fields)]
    return {'expected_records': len(values), 'reported_records': len(reported), 'complete': bool(values) and len(values) == len(reported),
            'reported_sum': {k: sum(v[k] for v in reported) for k in fields}}


def main():
    config = settings(); database = Database(config); manager = OpenVikingManager(config)
    output = {'scope': 'all original frozen attempts and all recovery attempts; includes quota failures; not only recovered quality subset',
              'groups': {}, 'embedding_query_usage': None, 'embedding_separate_elapsed_ms': None,
              'maintenance_usage': None, 'total_currency_cost': None,
              'limitations': ['Task token_usage covers only reported task activity; it omits standalone query embeddings.',
                  'Failed provider calls can lack usage. Successful-chat totals do not establish full billed totals.',
                  'Server processing_seconds is reported task duration; maintenance created-to-updated includes queueing.',
                  'No per-phase LLM/embedding latency split is inferred from a task duration.']}
    try:
        for campaign in ('frozen', 'frozen-resume-20261030'):
            manifest = read(campaign + '/manifest.json')
            predictions = read(campaign + '/predictions.json')
            for group in ('baseline', 'openviking'):
                users = [UUID(pair[group]) for pair in manifest['identities']]
                final_ids = {UUID(r['response']['message_id']) for r in predictions if r['group'] == group and not r['error_code']}
                with database.sessions() as db:
                    messages = list(db.scalars(select(ChatMessage).where(ChatMessage.user_id.in_(users), ChatMessage.role == 'assistant')))
                    jobs = list(db.scalars(select(MemorySyncJob).where(MemorySyncJob.user_id.in_(users))))
                    calls = list(db.scalars(select(ModelCall).where(ModelCall.user_id.in_(users))))
                row = {'model_call_status_counts': dict(Counter(c.status for c in calls)),
                       'model_calls_missing_usage': sum(c.total_tokens is None for c in calls)}
                for phase, selected in [('final_answers', [m for m in messages if m.id in final_ids]),
                                        ('preparation_answers', [m for m in messages if m.id not in final_ids])]:
                    usage_values = []
                    durations = []
                    for message in selected:
                        ledger = (message.run_metadata or {}).get('usage_by_model', {})
                        values = list(ledger.values())
                        usage_values.append({key: sum(v[key] for v in values) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
                            if values and all(v.get('complete') and all(numeric(v.get(k)) for k in ('input_tokens','output_tokens','total_tokens')) for v in values) else None)
                        elapsed = (message.run_metadata or {}).get('elapsed_ms')
                        if numeric(elapsed): durations.append(elapsed)
                    row[phase] = {'messages': len(selected), 'usage': totals(usage_values, ('input_tokens','output_tokens','total_tokens')),
                                  'reported_elapsed_ms': {'count': len(durations), 'sum': sum(durations),
                                                          'p95': percentile(durations, .95), 'includes': 'full agent execution'}}
                captures = [j for j in jobs if j.kind == 'capture']
                task_jobs = [j for j in captures if j.receipt.get('task_id')]
                def task_usage(job):
                    item = {'job_id': str(job.id), 'state': job.state, 'task_id': job.receipt['task_id'], 'error_code': None}
                    try:
                        task = manager.request(job.user_id, job.generation, 'GET', '/api/v1/tasks/' + job.receipt['task_id'],
                                               missing_ok=True, background=True)
                        if task is None:
                            item.update(error_code='TASK_RECEIPT_UNAVAILABLE', token_usage=None, processing_seconds=None)
                        else:
                            item.update(task_status=task.get('status'), processing_seconds=task.get('processing_seconds'),
                                        token_usage=(task.get('result') or {}).get('token_usage'))
                    except Exception as error:
                        item.update(error_code=getattr(error, 'code', type(error).__name__), token_usage=None, processing_seconds=None)
                    return item
                with ThreadPoolExecutor(max_workers=4) as pool:
                    task_rows = list(pool.map(task_usage, task_jobs))
                task_tokens = [t.get('token_usage') or {} for t in task_rows]
                durations = [t['processing_seconds'] for t in task_rows if numeric(t.get('processing_seconds'))]
                row['automatic_extraction'] = {
                    'capture_jobs': len(captures), 'capture_state_counts': dict(Counter(j.state for j in captures)),
                    'jobs_with_task_receipt': len(task_jobs), 'unavailable_receipts': sum(bool(t['error_code']) for t in task_rows),
                    'reported_llm_usage': totals([t.get('llm') for t in task_tokens], ('prompt_tokens','completion_tokens','total_tokens')),
                    'reported_task_embedding_usage': totals([t.get('embedding') for t in task_tokens], ('total_tokens',)),
                    'reported_processing_seconds': {'count': len(durations), 'sum': sum(durations), 'mean': mean(durations) if durations else None,
                                                     'p95': percentile(durations, .95)}, 'task_rows': task_rows}
                maintenance = [j for j in jobs if j.kind in ('delete', 'purge')]
                completed = [j for j in maintenance if j.state == 'succeeded']
                durations = [(aware(j.updated_at) - aware(j.created_at)).total_seconds() * 1000 for j in completed]
                row['maintenance'] = {'state_counts': dict(Counter(j.state for j in maintenance)),
                                      'completed_queue_to_finish_ms': {'count': len(durations), 'sum': sum(durations), 'p95': percentile(durations,.95)},
                                      'usage': None}
                output['groups'][campaign + '/' + group] = row
                print(json.dumps({'usage_campaign': campaign, 'group': group, 'capture_jobs': len(captures), 'task_receipts': len(task_rows)}), flush=True)
        (LOCAL / 'phase-usage.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    finally:
        manager.close(); database.close()


if __name__ == '__main__':
    main()
