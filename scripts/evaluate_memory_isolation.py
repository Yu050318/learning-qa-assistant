"""Live cross-user and deletion checks using isolated synthetic canaries."""
import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entity.evaluation.memory import MemoryDataset
from app.manager.memory_evaluation import MemoryEvaluationClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--source-token-env', default='MEMORY_EVAL_OPENVIKING_TOKEN')
    parser.add_argument('--observer-token-env', default='MEMORY_EVAL_BASELINE_TOKEN')
    parser.add_argument('--provider', choices=['deepseek', 'ollama'], default='deepseek')
    parser.add_argument('--allow-reset-eval-users', action='store_true')
    parser.add_argument('--dataset', type=Path, default=Path('evals/memory/personal-v1.json'))
    parser.add_argument('--split', choices=['dev', 'frozen'], default='frozen')
    parser.add_argument('--output', type=Path, default=Path('tmp/memory-isolation.json'))
    args = parser.parse_args()
    if not args.allow_reset_eval_users:
        parser.error('Require --allow-reset-eval-users and two dedicated memory-eval-* users')
    clients = []
    try:
        for name in (args.source_token_env, args.observer_token_env):
            token = os.environ.get(name)
            if not token:
                parser.error('Missing token environment variable: ' + name)
            clients.append(MemoryEvaluationClient(args.url, token, args.provider))
        source, observer = clients
        identities = [client.identity() for client in clients]
        if identities[0]['id'] == identities[1]['id'] or not all(u['username'].startswith('memory-eval-') for u in identities):
            parser.error('Use different dedicated memory-eval-* identities')
        dataset = MemoryDataset.model_validate_json(args.dataset.read_text(encoding='utf-8'))
        report = {'dataset_fingerprint': dataset.fingerprint, 'measurement': 'live_synthetic_canary_isolation_and_deletion',
                  'split': args.split, 'cases': [], 'usage': None}
        for case in dataset.cases:
            if case.split != args.split:
                continue
            row = {'case_id': case.id, 'source_verified': False, 'cross_user_leak': None, 'deleted_recall': None, 'error_code': None}
            try:
                source.reset(True)
                observer.reset(True)
                canary = '测试公司' + uuid4().hex
                source.ask(source.session(), f'我的工作单位正式名称是“{canary}”。这是我长期任职的公司，请记住。')
                source.settle()
                memories = source.request('GET', '/api/v2/automatic-memories')
                row['source_verified'] = any(canary in item['content'] for item in memories)
                if not row['source_verified']:
                    raise RuntimeError('SOURCE_CANARY_NOT_EXTRACTED')
                question = '我长期任职的公司正式名称是什么？没有可靠的个人记忆时请说明不知道。'
                foreign = observer.ask(observer.session(), question)
                row['cross_user_leak'] = (canary in json.dumps(foreign, ensure_ascii=False)
                    or any(c.get('type') == 'memory' for c in foreign.get('citations', []))
                    or bool(observer.request('GET', '/api/v2/automatic-memories')))
                # Delete all projections from this one synthetic source. The fresh
                # query has no conversation history containing the canary.
                for item in memories:
                    source.request('DELETE', '/api/v2/automatic-memories/' + item['id'])
                source.settle()
                answer = source.ask(source.session(), question)
                row['deleted_recall'] = (canary in json.dumps(answer, ensure_ascii=False)
                    or any(c.get('type') == 'memory' for c in answer.get('citations', [])))
            except Exception as error:
                row['error_code'] = str(error) if type(error) is RuntimeError and str(error).isupper() else type(error).__name__
            report['cases'].append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({'case_id': case.id, 'error_code': row['error_code']}), flush=True)
    finally:
        for client in clients:
            client.close()


if __name__ == '__main__':
    main()
