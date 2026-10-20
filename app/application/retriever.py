from uuid import UUID
from dataclasses import replace
from collections import defaultdict
from time import monotonic

from app.core.errors import AppError
from app.application.evidence import count_tokens
from app.domain.contracts import EmbeddingProvider, SearchHit, VectorStore


class RetrieverService:
    def __init__(self, embeddings: EmbeddingProvider, vectors: VectorStore, top_k: int, *, context_expander=None, reranker=None, hierarchy_expander=None, index_factory=None, settings_factory=None):
        self.embeddings = embeddings
        self.vectors = vectors
        self.top_k = top_k
        self.context_expander = context_expander
        self.reranker = reranker
        self.hierarchy_expander = hierarchy_expander
        self.index_factory, self.settings_factory = index_factory, settings_factory

    def for_settings(self, settings):
        return self.settings_factory(settings) if self.settings_factory else self

    def close(self):
        if self.reranker is not None:
            self.reranker.close()

    def retrieve(self, user_id: UUID, query: str, document_ids: list[UUID], *, deadline=None, document_owners=None, context_token_budget=None, embedding_query=None, indexes=()) -> list[SearchHit]:
        if embedding_query is not None and (not isinstance(embedding_query, str) or not embedding_query.strip() or len(embedding_query) > 6000):
            raise AppError("RAG_REWRITE_INVALID", "假设匹配文本无效或超长", 422)
        owners = document_owners or {did: user_id for did in document_ids}
        if set(owners) != set(document_ids):
            raise AppError("VECTOR_SCOPE_MISMATCH", "检索范围与文档归属不一致", 503)
        if indexes:
            return self._indexed(query, document_ids, owners, indexes, deadline, context_token_budget, embedding_query)
        groups = defaultdict(list)
        for did, owner in owners.items():
            groups[owner].append(did)

        def check_deadline():
            if document_owners is not None and deadline is not None and monotonic() >= deadline:
                raise AppError("AGENT_TIMEOUT", "检索时间预算耗尽", 504)

        check_deadline()
        vector = self.embeddings.embed_query(embedding_query if embedding_query is not None else query)
        hits = []
        for owner, ids in groups.items():
            check_deadline()
            batch = self.vectors.search(owner, ids, query, vector, self.top_k * 2)
            if any(hit.chunk.user_id != str(owner) or hit.chunk.document_id not in {str(did) for did in ids} for hit in batch):
                raise AppError("VECTOR_SCOPE_MISMATCH", "检索结果不属于获准文档归属", 503)
            hits.extend(batch)
        if len(groups) > 1:
            hits.sort(key=lambda hit: hit.score, reverse=True)
        return self._finish(query, hits, [(owner, ids, self.context_expander, self.hierarchy_expander) for owner, ids in groups.items()],
                            deadline, context_token_budget, check_deadline)

    def _indexed(self, query, document_ids, owners, indexes, deadline, context_token_budget, embedding_query):
        if self.index_factory is None or len(indexes) != len(document_ids) or {target.document_id for target in indexes} != set(document_ids):
            raise AppError("VECTOR_SCOPE_MISMATCH", "冻结索引范围不完整或读取器未配置", 503)
        grouped = defaultdict(list)
        for target in indexes:
            if owners[target.document_id] != target.user_id:
                raise AppError("VECTOR_SCOPE_MISMATCH", "冻结索引与授权归属不一致", 503)
            grouped[(target.user_id, target.collection_name, target.database_name, target.embedding_model,
                     target.embedding_dimension, target.connection_sha256)].append(target)
        def check_deadline():
            if deadline is not None and monotonic() >= deadline:
                raise AppError("AGENT_TIMEOUT", "冻结索引检索时间预算耗尽", 504)
        hits, groups, vectors = [], [], {}
        for key, targets in grouped.items():
            check_deadline()
            owner = key[0]
            ids = [target.document_id for target in targets]
            backend = self.index_factory(targets[0])
            embedding_key = key[3:6]
            if embedding_key not in vectors:
                vectors[embedding_key] = backend.embeddings.embed_query(embedding_query if embedding_query is not None else query)
            check_deadline()
            batch = backend.vectors.search(owner, ids, query, vectors[embedding_key], self.top_k * 2)
            if any(hit.chunk.user_id != str(owner) or hit.chunk.document_id not in {str(did) for did in ids} for hit in batch):
                raise AppError("VECTOR_SCOPE_MISMATCH", "冻结索引返回了范围外的证据", 503)
            hits.extend(batch)
            groups.append((owner, ids, backend.context_expander, backend.hierarchy_expander))
        if len(groups) > 1:
            hits.sort(key=lambda hit: hit.score, reverse=True)
        return self._finish(query, hits, groups, deadline, context_token_budget, check_deadline)

    def _finish(self, query, hits, groups, deadline, context_token_budget, check_deadline):
        unique = []
        seen = set()
        for hit in hits:
            normalized = "".join(hit.chunk.content.split())
            key = (hit.chunk.document_id, hit.chunk.excel_source.source_anchor, normalized) if hit.chunk.excel_source else normalized
            if not normalized or key in seen:
                continue
            seen.add(key)
            unique.append(hit)
        if self.reranker is not None and unique:
            unique = self.reranker.rerank(query, unique[:self.top_k * 2], self.top_k, deadline=deadline)
        else:
            unique = unique[:self.top_k]
        if not any(context or hierarchy for _, _, context, hierarchy in groups) or not unique:
            return unique
        try:
            expanded_by_id = {}
            for owner, ids, context_expander, hierarchy_expander in groups:
                selected = [hit for hit in unique if hit.chunk.user_id == str(owner) and hit.chunk.document_id in {str(did) for did in ids}]
                if selected:
                    check_deadline()
                    batch = (hierarchy_expander(owner, ids, selected, deadline=deadline, token_budget=context_token_budget)
                             if hierarchy_expander else context_expander(owner, ids, selected) if context_expander else selected)
                    if context_token_budget is not None:
                        context_token_budget = max(0, context_token_budget - sum(
                            count_tokens(parent.content) for parent in {h.parent_context.context_id: h.parent_context for h in batch if h.parent_context}.values()))
                    if len(batch) != len(selected) or any(before.chunk != after.chunk for before, after in zip(selected, batch)):
                        raise AppError("VECTOR_SCOPE_MISMATCH", "父块扩展改变了文档归属", 503)
                    expanded_by_id.update({hit.chunk.chunk_id: hit for hit in batch})
            expanded = [expanded_by_id[hit.chunk.chunk_id] for hit in unique]
        except AppError as error:
            if error.code not in {"MILVUS_UNAVAILABLE", "PARENT_CONTEXT_TIMEOUT", "HIERARCHY_UNAVAILABLE", "HIERARCHY_TIMEOUT"}:
                raise
            return [replace(hit, context_warning=error.code) for hit in unique]
        # Expansion can enrich, but cannot reorder, replace or add retrieved evidence.
        if len(expanded) != len(unique) or any(
            before.chunk != after.chunk or before.score != after.score
            or before.rerank_score != after.rerank_score or before.rerank_trace != after.rerank_trace
            or after.parent_context and not after.parent_context.contains(before.chunk)
            for before, after in zip(unique, expanded)
        ):
            raise AppError("VECTOR_SCOPE_MISMATCH", "父块上下文改变了实际命中的证据", 503)
        return expanded
