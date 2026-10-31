"""Isolated evaluation runtime; real credentials are never printed."""
import json
import logging
import os
from pathlib import Path
import secrets
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.config import Settings
from app.core.logging import configure_logging

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / 'tmp/openviking-live'


def settings():
    private = json.loads((LOCAL / 'runtime.json').read_text(encoding='utf-8'))
    return Settings().model_copy(update={
        'database_url': __import__('pydantic').SecretStr('postgresql+psycopg://architecture_test:local-test-only@127.0.0.1:15439/learningqa_architecture_test?sslmode=disable'),
        'database_require_tls': False, 'milvus_database': 'memory_eval_20261029',
        'milvus_collection': 'memory_eval_docs_v1', 'memory_collection': 'memory_eval_manual_v1',
        'jwt_secret': __import__('pydantic').SecretStr(private['jwt']), 'access_token_minutes': 60,
        'registration_enabled': True, 'auth_cookie_secure': False, 'langsmith_tracing': False,
        'model_center_enabled': False, 'document_versions_enabled': False,
        'document_publication_enabled': False, 'chat_runs_enabled': False,
        'sharing_enabled': False, 'workspaces_enabled': False, 'capability_center_enabled': False,
        'evaluation_enabled': False, 'hierarchy_enabled': False, 'knowledge_quality_enabled': False,
        'tool_approvals_enabled': False, 'sandbox_enabled': False, 'web_search_enabled': False,
        'rerank_enabled': False, 'sql_database_url': __import__('pydantic').SecretStr(''),
        'ingestion_mode': 'in_process', 'upload_dir': LOCAL / 'uploads', 'parsed_dir': LOCAL / 'parsed',
        'openviking_enabled': True, 'openviking_url': 'http://127.0.0.1:19340',
        'openviking_api_key': __import__('pydantic').SecretStr(private['gateway']),
        'openviking_account': 'learningqa-eval', 'openviking_poll_seconds': .5,
    })


def prepare(*, resume=False):
    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / 'data').mkdir(exist_ok=True)
    private = (json.loads((LOCAL / 'runtime.json').read_text(encoding='utf-8')) if resume else
               {'jwt': secrets.token_hex(32), 'gateway': secrets.token_hex(32)})
    if not resume:
        (LOCAL / 'runtime.json').write_text(json.dumps(private), encoding='utf-8')
    source = Settings()
    config = json.loads((ROOT / 'docker/openviking/ov.conf.example').read_text(encoding='utf-8'))
    config['server']['root_api_key'] = private['gateway']
    config['default_account'] = 'learningqa-eval'
    config['embedding']['dense'].update(api_key=source.dashscope_api_key.get_secret_value(),
        api_base=source.dashscope_base_url, model=source.embedding_model, dimension=source.embedding_dimension)
    config['vlm'].update(api_key=source.deepseek_api_key.get_secret_value(),
        api_base=source.deepseek_base_url.rstrip('/') + '/v1' if not source.deepseek_base_url.rstrip('/').endswith('/v1') else source.deepseek_base_url,
        model=source.deepseek_model, max_tokens=2048)
    (LOCAL / 'ov.conf').write_text(json.dumps(config), encoding='utf-8')
    print('Isolated model configuration prepared; no credentials displayed.')


def worker():
    from app.composition.container import Services
    configure_logging()
    services = Services(settings())
    try:
        while not (LOCAL / 'stop-worker').exists():
            try:
                if not services.automatic_memories.run_once():
                    time.sleep(.5)
            except Exception as error:
                logging.getLogger('rag').error('evaluation_memory_worker_failed', extra={'error_type': type(error).__name__})
                time.sleep(1)
    finally:
        services.close()


if __name__ == '__main__':
    import sys
    if sys.argv[1] == 'prepare':
        prepare()
    elif sys.argv[1] == 'resume-config':
        prepare(resume=True)
    elif sys.argv[1] == 'initialize':
        from app.dao.postgres.database import Database
        from app.dao.milvus import MilvusVectorStore
        database = Database(settings())
        database.initialize()
        database.close()
        MilvusVectorStore(settings()).initialize()
        print('Isolated PostgreSQL and Milvus initialized.')
    elif sys.argv[1] == 'api':
        import uvicorn
        from app.main import create_app
        uvicorn.run(create_app(settings()), host='127.0.0.1', port=18095, access_log=False, log_level='warning')
    elif sys.argv[1] == 'worker':
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: worker(), range(4)))
