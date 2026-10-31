"""Optional DashScope text reranking; provider text never replaces stored evidence."""

from dataclasses import replace
import json
from math import isfinite
from time import monotonic, perf_counter

import httpx

from app.core.errors import AppError
from app.entity.bo.contracts import RerankTrace, SearchHit


class TextReranker:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.client = httpx.Client(transport=transport, follow_redirects=False)

    def close(self):
        self.client.close()

    def rerank(self, query: str, hits: list[SearchHit], top_k: int, *, deadline=None) -> list[SearchHit]:
        if not hits:
            return []
        started = perf_counter()
        attempts = 0
        # Bound the submitted text, including repeated query cost, without changing citation excerpts.
        bounded_query = query[:200]
        character_limit = min(1000, max(1, 24000 // len(hits) - len(bounded_query)))
        documents = [hit.chunk.content[:character_limit] for hit in hits]
        truncated = bounded_query != query or any(text != hit.chunk.content for text, hit in zip(documents, hits))
        try:
            timeout = self.settings.rerank_timeout
            if deadline is not None:
                timeout = min(timeout, deadline - monotonic())
            if timeout <= 0:
                raise AppError("RERANK_TIMEOUT", "精排超时，沿用混合召回排序", 503)
            attempts = 1
            # No redirects or retries: each retrieval spends at most one provider request.
            with self.client.stream("POST", self.settings.rerank_endpoint,
                                    headers={"Authorization": f"Bearer {self.settings.rerank_api_key.get_secret_value()}"},
                                    json={"model": self.settings.rerank_model,
                                          "input": {"query": bounded_query, "documents": documents},
                                          "parameters": {"top_n": min(top_k, len(hits)), "return_documents": False}},
                                    timeout=timeout) as response:
                if response.status_code != 200:
                    code = {401: "RERANK_AUTH_ERROR", 403: "RERANK_AUTH_ERROR", 429: "RERANK_RATE_LIMITED"}.get(
                        response.status_code, "RERANK_UNAVAILABLE")
                    raise AppError(code, "精排服务不可用，沿用混合召回排序", 503)
                body = bytearray()
                for block in response.iter_bytes():
                    if (deadline is not None and monotonic() >= deadline) or perf_counter() - started >= timeout:
                        raise AppError("RERANK_TIMEOUT", "精排超时，沿用混合召回排序", 503)
                    body.extend(block)
                    if len(body) > 65536:
                        raise ValueError("response too large")
            payload = json.loads(body)
            container = payload.get("output", payload)
            results = container["results"]
            if not isinstance(results, list) or len(results) != min(top_k, len(hits)):
                raise ValueError("incomplete ranking")
            seen, ranked = set(), []
            for item in results:
                index, score = item["index"], item["relevance_score"]
                if (type(index) is not int or not 0 <= index < len(hits) or index in seen
                        or type(score) not in (int, float) or not isfinite(score) or not 0 <= score <= 1):
                    raise ValueError("invalid ranking")
                seen.add(index)
                ranked.append(replace(hits[index], rerank_score=float(score)))
            ranked.sort(key=lambda hit: -hit.rerank_score)
            usage = payload.get("usage", {})
            total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
            if type(total_tokens) is not int or total_tokens < 0:
                total_tokens = None
            trace = RerankTrace("applied", self.settings.rerank_model, len(hits), round((perf_counter() - started) * 1000, 2),
                                attempts, truncated, total_tokens=total_tokens)
            return [replace(hit, rerank_trace=trace) for hit in ranked]
        except httpx.TimeoutException:
            code = "RERANK_TIMEOUT"
        except httpx.HTTPError:
            code = "RERANK_UNAVAILABLE"
        except (ValueError, KeyError, TypeError, AttributeError):
            code = "RERANK_INVALID_RESPONSE"
        except AppError as error:
            code = error.code
        trace = RerankTrace("fallback", self.settings.rerank_model, len(hits), round((perf_counter() - started) * 1000, 2),
                            attempts, truncated, code)
        return [replace(hit, rerank_trace=trace) for hit in hits[:top_k]]
