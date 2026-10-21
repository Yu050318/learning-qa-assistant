import re
import json
import hashlib
from datetime import datetime, timezone
from dataclasses import asdict
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
        self.sources: dict[int, dict] = {}
        self.contexts: dict[str, dict] = {}
        self.context_tokens = 0
        self.warnings: set[str] = set()
        self.rerank_events: list[dict] = []
        self.hierarchy_events: list[dict] = []
        self.excluded_numbers: set[int] = set()
        self.keys: dict[tuple[str, str], int] = {}
        self.used_tokens = 0

    def snapshot(self):
        return {"token_budget": self.token_budget, "entries": self.entries, "sources": list(self.sources.values()),
            "contexts": self.contexts, "context_tokens": self.context_tokens, "warnings": sorted(self.warnings),
            "rerank_events": self.rerank_events, "hierarchy_events": self.hierarchy_events,
            "excluded_numbers": sorted(self.excluded_numbers), "keys": [[*key, number] for key, number in self.keys.items()],
            "used_tokens": self.used_tokens}

    def restore(self, state):
        if state["token_budget"] != self.token_budget:
            raise AppError("RUN_CHECKPOINT_DIVERGED", "证据预算与检查点不一致", 409)
        self.entries = state["entries"]
        self.sources = {source["number"]: source for source in state["sources"]}
        self.contexts, self.context_tokens = state["contexts"], state["context_tokens"]
        self.warnings, self.excluded_numbers = set(state["warnings"]), set(state["excluded_numbers"])
        self.rerank_events, self.hierarchy_events = state["rerank_events"], state["hierarchy_events"]
        self.keys = {(kind, identity): number for kind, identity, number in state["keys"]}
        self.used_tokens = state["used_tokens"]

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
        source = {"number": number, "type": citation["type"], "title": title[:500], "content": content}
        self.sources[number] = source
        return source, False

    def add_web_page(self, page) -> dict:
        key = ("web", normalized_url(page.url))
        number = self.keys.get(key)
        if number is None:
            return self.add_web(WebSearchBatch([page]))
        previous = self.sources[number]
        available = self.token_budget - self.used_tokens + count_tokens(previous["content"])
        content = page.content[:12000].strip()
        while content and count_tokens(content) > available:
            content = content[:len(content) // 2]
        if not content:
            return {"status": "empty", "sources": [], "existing_numbers": [number], "truncated": True}
        self.used_tokens += count_tokens(content) - count_tokens(previous["content"])
        source = {**previous, "content": content, "title": page.title[:500]}
        self.sources[number] = source
        self.entries[number - 1].update(excerpt=content[:500], title=page.title[:500])
        return {"status": "ok", "sources": [source], "existing_numbers": [], "truncated": len(content) < len(page.content)}

    def add_knowledge(self, hits: list[SearchHit]) -> dict:
        if any(hit.parent_context and not hit.parent_context.contains(hit.chunk) for hit in hits):
            raise AppError("VECTOR_SCOPE_MISMATCH", "父块上下文不包含本轮命中的原始证据", 503)
        trace = hits[0].rerank_trace if hits else None
        if trace is not None:
            self.rerank_events.append(asdict(trace))
        sources, existing, truncated = [], [], False
        for hit in hits:
            if hit.hierarchy_trace and hit.hierarchy_trace not in self.hierarchy_events:
                self.hierarchy_events.append(dict(hit.hierarchy_trace))
            citation = {
                "type": "knowledge", "document_id": hit.chunk.document_id, "chunk_id": hit.chunk.chunk_id,
                "source_name": hit.chunk.source_name, "page_number": hit.chunk.page_number,
                "section": hit.chunk.section, "score": hit.score,
            }
            if hit.chunk.excel_source:
                citation["excel_source"] = asdict(hit.chunk.excel_source)
            if hit.rerank_score is not None:
                citation["rerank_score"] = hit.rerank_score
            source, duplicate = self._add(("knowledge", hit.chunk.chunk_id), citation, hit.chunk.source_name, hit.chunk.content)
            if source and hit.chunk.excel_source:
                source["excel_source"] = asdict(hit.chunk.excel_source)
            if duplicate:
                existing.append(self.keys[("knowledge", hit.chunk.chunk_id)])
            elif source:
                sources.append(source)
            else:
                truncated = True
        context_ids = set()
        warnings = {hit.context_warning for hit in hits if hit.context_warning}
        if trace and trace.error_code:
            warnings.add(trace.error_code)
        for hit in hits:
            parent = hit.parent_context
            number = self.keys.get(("knowledge", hit.chunk.chunk_id))
            if parent is None or number is None:
                continue
            context_id = parent.context_id
            if context_id not in self.contexts:
                tokens = count_tokens(parent.content)
                # ponytail: at most one third of evidence budget for unnumbered context; leaves keep priority.
                if tokens > min(self.token_budget - self.used_tokens, self.token_budget // 3 - self.context_tokens):
                    warnings.add("PARENT_CONTEXT_BUDGET_EXCEEDED")
                    continue
                self.contexts[context_id] = {"context_id": context_id, "type": "context_only", "for_numbers": [],
                                             "title": hit.chunk.source_name, "content": parent.content}
                self.used_tokens += tokens
                self.context_tokens += tokens
            if number not in self.contexts[context_id]["for_numbers"]:
                self.contexts[context_id]["for_numbers"].append(number)
            context_ids.add(context_id)
        self.warnings.update(warnings)
        result = {"status": "ok" if sources or existing else "empty", "sources": sources, "existing_numbers": existing,
                "truncated": truncated, "contexts": [self.contexts[key] for key in sorted(context_ids)], "warnings": sorted(warnings)}
        if trace is not None:
            result["rerank"] = asdict(trace)
        return result

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

    def add_tool(self, name, version, data):
        content = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False)
        key = ("tool", name + "/" + str(version) + "/" + hashlib.sha256(content.encode()).hexdigest())
        source, duplicate = self._add(key, {"type": "tool", "tool_name": name, "tool_version": version,
            "retrieved_at": datetime.now(timezone.utc).isoformat()}, name, content)
        if source:
            source.update(tool_name=name, tool_version=version)
        return {"status": "ok" if source or duplicate else "empty", "sources": [source] if source else [],
            "existing_numbers": [self.keys[key]] if duplicate else [], "truncated": len(content) > 12000 or source is None and not duplicate}

    def add_memories(self, memories: list[dict]) -> dict:
        sources, existing, truncated = [], [], False
        for memory in memories:
            key = ("memory", str(memory["memory_id"]))
            citation = {
                "type": "memory", "memory_id": str(memory["memory_id"]), "score": memory["score"],
            }
            source, duplicate = self._add(key, citation, "长期记忆", memory["content"])
            if duplicate:
                existing.append(self.keys[key])
            elif source:
                sources.append(source)
            else:
                truncated = True
        return {"status": "ok" if sources or existing else "empty", "sources": sources, "existing_numbers": existing, "truncated": truncated}

    def add_sql(self, result: dict) -> dict:
        content = json.dumps(result, ensure_ascii=False, default=str)
        key = ("sql", hashlib.sha256(content.encode()).hexdigest())
        citation = {"type": "sql", "source_name": result["source_name"], "query": result["query"],
                    "columns": result["columns"], "row_count": len(result["rows"]), "truncated": result["truncated"]}
        source, duplicate = self._add(key, citation, result["source_name"], content)
        if source:
            preview = "\n".join("；".join(f"{column}：{value}" for column, value in zip(result["columns"], row))
                                for row in result["rows"]) or "查询结果为空（0 行）"
            self.entries[source["number"] - 1]["excerpt"] = preview[:500]
        return {"status": "ok" if source or duplicate else "empty", "sources": [source] if source else [],
                "existing_numbers": [self.keys[key]] if duplicate else [], "truncated": source is None and not duplicate}

    def validate(self, kind: str, answer: str, search_mode: str = "auto") -> list[dict]:
        numbers = sorted({int(value) for value in re.findall(r"\[(\d+)\]", answer)})
        if kind == "clarification":
            if numbers:
                raise AppError("INVALID_CITATION", "澄清问题不能声明事实引用", 502)
            return []
        by_number = {entry["number"]: entry for entry in self.entries if entry["number"] not in self.excluded_numbers}
        non_memory_entries = [entry for entry in by_number.values() if entry["type"] != "memory"]
        if non_memory_entries and kind != "grounded":
            raise AppError("INVALID_CITATION", "已有检索证据时必须生成带引用的 grounded 回答", 502)
        if any(number not in by_number for number in numbers):
            raise AppError("INVALID_CITATION", "回答包含未知引用", 502)
        citations = [by_number[number] for number in numbers]
        if kind == "grounded" and not citations:
            raise AppError("INVALID_CITATION", "有依据的回答必须包含引用", 502)
        expected = {"knowledge": "knowledge", "web": "web", "sql": "sql"}.get(search_mode)
        allowed = {expected, "memory"} | ({"tool"} if search_mode == "web" else set())
        if expected and any(citation["type"] not in allowed for citation in citations):
            raise AppError("INVALID_CITATION", "回答引用了当前模式未授权的来源", 502)
        return citations
