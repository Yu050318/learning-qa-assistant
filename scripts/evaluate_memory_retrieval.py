"""Controlled Recall@3: index identical labelled memories in Milvus and OpenViking."""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean
from time import perf_counter
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.composition.container import Services
from app.core.config import Settings
from app.entity.do.models import User
from app.entity.evaluation.memory import MemoryDataset
from app.manager.transactions import transaction_scope
from app.service.memory_evaluation import interval


def run_trial(services, baseline_user_id, openviking_user_id, case):
    for row in services.memories.list(baseline_user_id):
        services.memories.delete(baseline_user_id, row.id)
    corpus = case.gold_memories + ['我养护一盆绿萝。', '我习惯在周末读历史书。', '我喜欢步行散步。', '我正在整理照片。', '我偏好安静的阅读环境。']
    gold_ids = set()
    for index, content in enumerate(corpus):
        row = services.memories.create(baseline_user_id, content, created_via='management_ui')
        if row.status != 'ready':
            raise RuntimeError('BASELINE_INDEX_INCOMPLETE')
        if index < len(case.gold_memories):
            gold_ids.add(row.id)
    started = perf_counter()
    hits = services.memories.search(baseline_user_id, case.question)[:3]
    baseline_ms = (perf_counter() - started) * 1000
    baseline = len({row['memory_id'] for row in hits} & gold_ids) / len(gold_ids)
    manager = services.openviking
    root = f'viking://user/{manager.identity(openviking_user_id, 1)}/memories/preferences/controlled-retrieval'
    manager.remove(openviking_user_id, 1, root)
    manager.request(openviking_user_id, 1, 'POST', '/api/v1/fs/mkdir', background=True, json={'uri': root})
    operations = [{'uri': root + f'/item-{index}.md', 'content': content, 'mode': 'upsert'} for index, content in enumerate(corpus)]
    indexed = manager.request(openviking_user_id, 1, 'POST', '/api/v1/content/batch-write', background=True,
        json={'root_uri': root, 'operations': operations, 'wait': True})
    if not isinstance(indexed, dict) or indexed.get('vector_status') != 'complete' or indexed.get('semantic_status') not in {'complete', 'skipped'}:
        raise RuntimeError('OPENVIKING_INDEX_INCOMPLETE')
    started = perf_counter()
    found = manager.request(openviking_user_id, 1, 'POST', '/api/v1/search/find', background=True,
        json={'query': case.question, 'target_uri': root, 'context_type': 'memory', 'level': 2, 'limit': 3})
    viking_ms = (perf_counter() - started) * 1000
    gold_uris = {root + f'/item-{index}.md' for index in range(len(case.gold_memories))}
    # Any unexpected scope is a failed trial, not a credited retrieval hit.
    uris = {manager.memory_uri(openviking_user_id, 1, item['uri']) for item in found['memories'][:3]}
    if any(not uri.startswith(root + '/') for uri in uris):
        raise RuntimeError('RETRIEVAL_SCOPE_INVALID')
    return {'baseline_recall_at_3': baseline, 'openviking_recall_at_3': len(uris & gold_uris) / len(gold_uris),
            'gold_count': len(gold_ids), 'baseline_retrieval_ms': baseline_ms, 'openviking_retrieval_ms': viking_ms}


def summarize_trials(rows):
    result = {'executed': len(rows), 'execution_failures': sum(row['error_code'] is not None for row in rows)}
    for group in ('baseline', 'openviking'):
        clusters = defaultdict(list)
        for row in rows:
            clusters[row['case_id']].append(row.get(group + '_recall_at_3', 0) if not row['error_code'] else 0)
        values = [mean(values) for values in clusters.values()]
        result[group + '_recall_at_3'] = mean(values) if values else None
        result[group + '_ci95'] = interval(values)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-user-id', type=UUID, required=True)
    parser.add_argument('--openviking-user-id', type=UUID, required=True)
    parser.add_argument('--allow-reset-eval-users', action='store_true')
    parser.add_argument('--dataset', type=Path, default=Path('evals/memory/personal-v1.json'))
    parser.add_argument('--split', choices=['dev', 'frozen'], default='frozen')
    parser.add_argument('--output', type=Path, default=Path('tmp/memory-retrieval.json'))
    args = parser.parse_args()
    if not args.allow_reset_eval_users or args.baseline_user_id == args.openviking_user_id:
        parser.error('Require two distinct dedicated memory-eval-* users and --allow-reset-eval-users')
    settings = Settings()
    if not settings.openviking_enabled:
        parser.error('Configure OpenViking explicitly before running a live backend comparison')
    services = Services(settings)
    results = []
    try:
        with transaction_scope(services.database) as transaction:
            users = [transaction.load(User, uid) for uid in (args.baseline_user_id, args.openviking_user_id)]
            if not all(user and user.username.startswith('memory-eval-') for user in users):
                parser.error('Ordinary user memories cannot be reset by this experiment')
        dataset = MemoryDataset.model_validate_json(args.dataset.read_text(encoding='utf-8'))
        eligible = [case for case in dataset.cases if case.split == args.split and case.gold_memories]
        for case in eligible:
            for trial in range(1, 4):
                row = {'case_id': case.id, 'trial': trial, 'error_code': None}
                try:
                    row.update(run_trial(services, args.baseline_user_id, args.openviking_user_id, case))
                except Exception as error:
                    row['error_code'] = str(error) if type(error) is RuntimeError and str(error).isupper() else type(error).__name__
                results.append(row)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({'dataset_fingerprint': dataset.fingerprint, 'measurement': 'controlled_backend_retrieval_not_answer_accuracy',
                    'top_k': 3, 'expected_trials': len(eligible) * 3, 'summary': summarize_trials(results),
                    'cases': results, 'embedding_usage': None, 'indexing_usage': None}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                print(json.dumps({'case_id': case.id, 'trial': trial, 'error_code': row['error_code']}), flush=True)
    finally:
        services.close()


if __name__ == '__main__':
    main()
