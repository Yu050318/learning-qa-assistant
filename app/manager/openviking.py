"""Bounded OpenViking HTTP adapter. Trusted identity comes only from our backend."""
import json
import math
from urllib.parse import unquote

import httpx

from app.core.errors import AppError


class OpenVikingManager:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(base_url=settings.openviking_url.rstrip('/'), transport=transport,
                                   follow_redirects=False, trust_env=False)

    @staticmethod
    def identity(user_id, generation):
        return f"{user_id.hex}-g{generation}"

    def request(self, user_id, generation, method, path, *, missing_ok=False, background=False, **kwargs):
        headers = {"X-API-Key": self.settings.openviking_api_key.get_secret_value(),
                   "X-OpenViking-Account": self.settings.openviking_account,
                   "X-OpenViking-User": self.identity(user_id, generation), "X-OpenViking-Role": "user"}
        try:
            with self.client.stream(method, path, headers=headers,
                    timeout=self.settings.openviking_worker_timeout if background else self.settings.openviking_recall_timeout, **kwargs) as response:
                if response.status_code == 404 and missing_ok:
                    return None
                if response.status_code >= 400 or response.is_redirect:
                    raise AppError("OPENVIKING_UNAVAILABLE", "记忆服务暂不可用", 503)
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 1_048_576:
                        raise ValueError("response too large")
                data = json.loads(content)
                if not isinstance(data, dict) or data.get('status') not in {'ok', 'accepted', 'skipped'} or 'result' not in data:
                    raise ValueError("invalid envelope")
                return data['result']
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AppError("OPENVIKING_UNAVAILABLE", "记忆服务返回失败，已保留原问答能力", 503) from exc

    def memory_uri(self, user_id, generation, uri):
        if not isinstance(uri, str) or len(uri) > 1024:
            raise AppError("OPENVIKING_SCOPE_INVALID", "记忆来源不属于当前用户", 502)
        prefix = f"viking://user/{self.identity(user_id, generation)}/memories/"
        canonical = uri.replace('viking://~/memories/', prefix, 1)
        suffix = canonical.removeprefix(prefix)
        if (not canonical.startswith(prefix) or any(p in {'.', '..', ''} for p in unquote(suffix).split('/'))
                or '\\' in suffix or '?' in suffix or '#' in suffix
                or not (suffix == 'profile.md' or suffix.startswith(('preferences/', 'entities/')))):
            raise AppError("OPENVIKING_SCOPE_INVALID", "记忆来源不属于当前用户", 502)
        return canonical

    def find(self, user_id, generation, query, top_k):
        result = self.request(user_id, generation, 'POST', '/api/v1/search/find',
            json={'query': query, 'target_uri': 'viking://~/memories', 'context_type': 'memory', 'limit': top_k, 'level': 2})
        if not isinstance(result, dict) or not isinstance(result.get('memories'), list):
            raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆检索返回无效结果', 502)
        hits = []
        for item in result['memories'][:top_k]:
            if not isinstance(item, dict):
                raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆检索返回无效记录', 502)
            uri = self.memory_uri(user_id, generation, item.get('uri'))
            score = item.get('score')
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆检索返回无效分数', 502)
            hits.append((uri, float(score)))
        return hits

    def read_diff(self, user_id, generation, archive_uri):
        prefix = f'viking://user/{self.identity(user_id, generation)}/sessions/'
        if (not isinstance(archive_uri, str) or not archive_uri.startswith(prefix)
                or any(p in {'.', '..', ''} for p in unquote(archive_uri[len(prefix):]).split('/'))):
            raise AppError('OPENVIKING_SCOPE_INVALID', '记忆提交来源无效', 502)
        content = self.request(user_id, generation, 'GET', '/api/v1/content/read', background=True,
                               params={'uri': archive_uri.rstrip('/') + '/memory_diff.json'})
        try:
            diff = json.loads(content)
            if not isinstance(diff.get('operations'), dict):
                raise ValueError('invalid diff')
            return diff['operations']
        except (ValueError, TypeError, AttributeError) as exc:
            raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆变更记录无效', 502) from exc

    def remove(self, user_id, generation, uri=None):
        target = self.memory_uri(user_id, generation, uri) if uri else 'viking://~/memories'
        return self.request(user_id, generation, 'DELETE', '/api/v1/fs', missing_ok=True, background=True,
                            params={'uri': target, 'recursive': 'true'})

    def remove_session(self, user_id, generation, job_id):
        return self.request(user_id, generation, 'DELETE', f'/api/v1/sessions/rag-{job_id}',
                            missing_ok=True, background=True)

    def close(self):
        self.client.close()
