"""Validate the memory corpus, run paired trials, or score saved predictions and reviews."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entity.evaluation.memory import MemoryDataset, prediction_digest
from app.manager.memory_evaluation import MemoryEvaluationClient
from app.service.memory_evaluation import markdown, run_scenario, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=Path('evals/memory/personal-v1.json'))
    parser.add_argument('--split', choices=['dev', 'frozen'], default='frozen')
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--baseline-url', default='http://127.0.0.1:8000')
    parser.add_argument('--openviking-url', default='http://127.0.0.1:8000')
    parser.add_argument('--baseline-token-env', default='MEMORY_EVAL_BASELINE_TOKEN')
    parser.add_argument('--openviking-token-env', default='MEMORY_EVAL_OPENVIKING_TOKEN')
    parser.add_argument('--allow-reset-eval-users', action='store_true')
    parser.add_argument('--provider', choices=['deepseek', 'ollama'], default='deepseek')
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--isolation', type=Path)
    parser.add_argument('--output', type=Path, default=Path('tmp/memory-evaluation'))
    args = parser.parse_args()
    dataset = MemoryDataset.model_validate_json(args.dataset.read_text(encoding='utf-8'))
    cases = [case for case in dataset.cases if case.split == args.split]
    print(json.dumps({'dataset_fingerprint': dataset.fingerprint, 'cases': len(cases), 'repeats': args.repeats}))
    predictions = json.loads(args.predictions.read_text(encoding='utf-8')) if args.predictions else []
    args.output.mkdir(parents=True, exist_ok=True)
    if args.run:
        if not args.allow_reset_eval_users:
            parser.error('Live trials reset memories; require --allow-reset-eval-users and two dedicated memory-eval-* users')
        clients = {}
        try:
            for group, url, name in [('baseline', args.baseline_url, args.baseline_token_env), ('openviking', args.openviking_url, args.openviking_token_env)]:
                token = os.environ.get(name)
                if not token:
                    parser.error('Missing evaluation token environment variable: ' + name)
                clients[group] = MemoryEvaluationClient(url, token, args.provider)
            identities = [client.identity() for client in clients.values()]
            if identities[0]['id'] == identities[1]['id'] or not all(u['username'].startswith('memory-eval-') for u in identities):
                parser.error('Use two different dedicated memory-eval-* identities; ordinary users cannot be reset')
            exhausted = False
            for trial in range(1, args.repeats + 1):
                if exhausted:
                    break
                for index, case in enumerate(cases):
                    if exhausted:
                        break
                    order = ('baseline', 'openviking') if (index + trial) % 2 else ('openviking', 'baseline')
                    for group in order:
                        row = run_scenario(clients[group], case, group, trial)
                        predictions.append(row)
                        (args.output / 'predictions.json').write_text(json.dumps(predictions, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                        print(json.dumps({'case_id': case.id, 'group': group, 'trial': trial, 'error_code': row['error_code']}), flush=True)
                        if row['error_code'] == 'MODEL_BALANCE_EXHAUSTED':
                            exhausted = True
                            print('Provider balance exhausted; remaining requests were not attempted.', flush=True)
                            break
        finally:
            for client in clients.values():
                client.close()
    reviews = json.loads(args.reviews.read_text(encoding='utf-8')) if args.reviews else []
    if predictions and not args.reviews:
        template = [{'case_id': row['case_id'], 'trial': row['trial'], 'group': row['group'],
                     'reviewer': 'human', 'prediction_sha256': prediction_digest(row),
                     'correct': None, 'unsupported_personal_fact': None, 'used_updated_fact': None}
                    for row in predictions if not row.get('error_code')]
        # Null labels deliberately fail validation until a reviewer fills them in.
        (args.output / 'review-template.json').write_text(json.dumps(template, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    isolation = json.loads(args.isolation.read_text(encoding='utf-8')) if args.isolation else None
    report = summarize(dataset, predictions, reviews, repeats=args.repeats, split=args.split, isolation=isolation)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.output / 'report.md').write_text(markdown(report), encoding='utf-8')
    print('Quality gate: ' + ('PASS' if report['quality_gate_passed'] else 'NOT PASSED / INCOMPLETE'))


if __name__ == '__main__':
    main()
