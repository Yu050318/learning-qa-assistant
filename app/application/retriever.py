from uuid import UUID
from dataclasses import replace
from collections import defaultdict
from time import monotonic

from app.core.errors import AppError
from app.domain.contracts import EmbeddingProvider, SearchHit, VectorStore


class RetrieverService:
    def __init__(self, embeddings: EmbeddingProvider, vectors: VectorStore, top_k: int, *, context_expander=None, reranker=None):
        self.embeddings = embeddings
        self.vectors = vectors
        self.top_k = top_k
        self.context_expander = context_expander
        self.reranker = reranker

    def retrieve(self, user_id: UUID, query: str, document_ids: list[UUID], *, deadline=None, document_owners=None) -> list[SearchHit]:
        owners = document_owners or {did: user_id for did in document_ids}
        if set(owners) != set(document_ids):
            raise AppError("VECTOR_SCOPE_MISMATCH", "检索范围与文档归属不一致", 503)
        groups = defaultdict(list)
        for did, owner in owners.items():
            groups[owner].append(did)

        def check_deadline():
            if document_owners is not None and deadline is not None and monotonic() >= deadline:
                raise AppError("AGENT_TIMEOUT", "检索时间预算耗尽", 504)

        check_deadline()
        vector = self.embeddings.embed_query(query)
        hits = []
        for owner, ids in groups.items():
            check_deadline()
            batch = self.vectors.search(owner, ids, query, vector, self.top_k * 2)
            if any(hit.chunk.user_id != str(owner) or hit.chunk.document_id not in {str(did) for did in ids} for hit in batch):
                raise AppError("VECTOR_SCOPE_MISMATCH", "检索结果不属于获准文档归属", 503)
            hits.extend(batch)
        if len(groups) > 1:
            hits.sort(key=lambda hit: hit.score, reverse=True)
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
        if self.context_expander is None or not unique:
            return unique
        try:
            expanded_by_id = {}
            for owner, ids in groups.items():
                selected = [hit for hit in unique if hit.chunk.user_id == str(owner)]
                if selected:
                    check_deadline()
                    batch = self.context_expander(owner, ids, selected)
                    if len(batch) != len(selected) or any(before.chunk != after.chunk for before, after in zip(selected, batch)):
                        raise AppError("VECTOR_SCOPE_MISMATCH", "父块扩展改变了文档归属", 503)
                    expanded_by_id.update({hit.chunk.chunk_id: hit for hit in batch})
            expanded = [expanded_by_id[hit.chunk.chunk_id] for hit in unique]
        except AppError as error:
            if error.code not in {"MILVUS_UNAVAILABLE", "PARENT_CONTEXT_TIMEOUT"}:
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
