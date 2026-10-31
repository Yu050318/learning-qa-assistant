"""HTTP transport used only by the isolated evaluation command."""
from time import monotonic, sleep
from uuid import uuid4

import httpx


class MemoryEvaluationClient:
    def __init__(self, url, token, provider='deepseek', transport=None):
        self.provider = provider
        self.client = httpx.Client(base_url=url.rstrip('/'), headers={'Authorization': 'Bearer ' + token},
                                   timeout=180, follow_redirects=False, transport=transport, trust_env=False)

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        if response.status_code >= 300:
            try:
                code = response.json().get('error', {}).get('code')
            except (ValueError, AttributeError):
                code = None
            if code == 'MODEL_BALANCE_EXHAUSTED':
                raise RuntimeError(code)
            raise RuntimeError('EVALUATION_HTTP_' + str(response.status_code))
        return response.json() if response.content else None

    def identity(self):
        return self.request('GET', '/api/v2/auth/me')

    def settle(self):
        deadline = monotonic() + 620
        while monotonic() < deadline:
            status = self.request('GET', '/api/v2/memory-settings')
            if status['failed_jobs']:
                raise RuntimeError('MEMORY_SYNC_FAILED_OR_UNCERTAIN')
            if status['pending_jobs'] == 0:
                return
            sleep(1)
        raise RuntimeError('MEMORY_SYNC_TIMEOUT')

    def reset(self, automatic):
        for memory in self.request('GET', '/api/v2/memories'):
            self.request('DELETE', '/api/v2/memories/' + memory['id'])
        status = self.request('GET', '/api/v2/memory-settings')
        if automatic and not status['available']:
            raise RuntimeError('AUTOMATIC_MEMORY_UNAVAILABLE')
        if status['available']:
            self.request('DELETE', '/api/v2/automatic-memories')
            self.request('PATCH', '/api/v2/memory-settings', json={'automatic_enabled': automatic})
            self.settle()

    def remember(self, content):
        return self.request('POST', '/api/v2/memories', json={'content': content})

    def ask(self, session_id, question):
        return self.request('POST', f'/api/v2/sessions/{session_id}/messages',
            json={'question': question, 'search_mode': 'knowledge', 'model_provider': self.provider},
            headers={'Idempotency-Key': str(uuid4())})

    def session(self):
        return self.request('POST', '/api/v2/sessions', json={'title': '个人记忆固定评测'})['id']

    def close(self):
        self.client.close()
