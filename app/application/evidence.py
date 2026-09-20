import re
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import tiktoken

from app.core.errors import AppError
from app.domain.contracts import SearchHit, WebSearchBatch


@lru_cache(maxsize=1)
def _encoding():
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(*values) -> int:
    text = "\n".join(str(value) for value in values if value is not None)
    encoding = _encoding()
    if encoding is not None:
        return len(encoding.encode(text, disallowed_special=()))
    # ponytail: conservative offline fallback; replace if the chosen model exposes a local tokenizer.
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def evidence_token_budget(context_budget: int) -> int:
    return min(6000, max(1000, context_budget // 2))


def normalized_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = host if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class EvidenceRegistry:
    def __init__(self, token_budget: int = 12000):
        self.token_budget = token_budget
        self.entries: list[dict] = []
        self.keys: dict[tuple[str, str], int] = {}
        self.used_tokens = 0

    def _add(self, key: tuple[str, str], citation: dict, title: str, content: str) -> tuple[dict | None, bool]:
        if key in self.keys:
            return None, True
        content = content[:12000].strip()
        tokens = count_tokens(content)
        if not content or self.used_tokens + tokens > self.token_budget:
            return None, False
        number = len(self.entries) + 1
        citation = {**citation, "number": number, "excerpt": content[:500]}
        self.entries.append(citation)
        self.keys[key] = number
        self.used_tokens += tokens
        return {"number": number, "type": citation["type"], "title": title[:500], "content": content}, False

    def add_knowledge(self, hits: list[SearchHit]) -> dict:
        sources, existing, truncated = [], [], False
        for hit in hits:
            citation = {
                "type": "knowledge", "document_id": hit.chunk.document_id, "chunk_id": hit.chunk.chunk_id,
                "source_name": hit.chunk.source_name, "page_number": hit.chunk.page_number,
                "section": hit.chunk.section, "score": hit.score,
            }
            source, duplicate = self._add(("knowledge", hit.chunk.chunk_id), citation, hit.chunk.source_name, hit.chunk.content)
            if duplicate:
                existing.append(self.keys[("knowledge", hit.chunk.chunk_id)])
            elif source:
                sources.append(source)
            else:
                truncated = True
        return {"status": "ok" if sources or existing else "empty", "sources": sources, "existing_numbers": existing, "truncated": truncated}

    def add_web(self, batch: WebSearchBatch) -> dict:
        sources, existing, truncated = [], [], False
        for result in batch.results:
            key = ("web", normalized_url(result.url))
            citation = {
                "type": "web", "title": result.title, "url": result.url,
                "retrieved_at": result.retrieved_at.isoformat(),
                "published_at": result.published_at.isoformat() if result.published_at else None,
                "score": result.score,
            }
            source, duplicate = self._add(key, citation, result.title, result.content)
            if duplicate:
                existing.append(self.keys[key])
            elif source:
                sources.append(source)
            else:
                truncated = True
        return {"status": "ok" if sources or existing else "empty", "sources": sources, "existing_numbers": existing, "truncated": truncated}

    def validate(self, kind: str, answer: str, search_mode: str = "auto") -> list[dict]:
        numbers = sorted({int(value) for value in re.findall(r"\[(\d+)\]", answer)})
        by_number = {entry["number"]: entry for entry in self.entries}
        if self.entries and kind != "grounded":
            raise AppError("INVALID_CITATION", "已有检索证据时必须生成带引用的 grounded 回答", 502)
        if any(number not in by_number for number in numbers):
            raise AppError("INVALID_CITATION", "回答包含未知引用", 502)
        citations = [by_number[number] for number in numbers]
        if kind == "grounded" and not citations:
            raise AppError("INVALID_CITATION", "有依据的回答必须包含引用", 502)
        expected = {"knowledge": "knowledge", "web": "web"}.get(search_mode)
        if expected and any(citation["type"] != expected for citation in citations):
            raise AppError("INVALID_CITATION", "回答引用了当前模式未授权的来源", 502)
        return citations
