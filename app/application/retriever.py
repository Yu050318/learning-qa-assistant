from uuid import UUID
from dataclasses import replace

from app.core.errors import AppError
from app.domain.contracts import EmbeddingProvider, SearchHit, VectorStore


class RetrieverService:
    def __init__(self, embeddings: EmbeddingProvider, vectors: VectorStore, top_k: int, *, context_expander=None):
        self.embeddings = embeddings
        self.vectors = vectors
        self.top_k = top_k
        self.context_expander = context_expander

    def retrieve(self, user_id: UUID, query: str, document_ids: list[UUID]) -> list[SearchHit]:
        vector = self.embeddings.embed_query(query)
        hits = self.vectors.search(user_id, document_ids, query, vector, self.top_k * 2)
        allowed = {str(document_id) for document_id in document_ids}
        unique = []
        seen = set()
        for hit in hits:
            if hit.chunk.user_id != str(user_id) or hit.chunk.document_id not in allowed:
                raise AppError("VECTOR_SCOPE_MISMATCH", "检索结果不属于当前用户范围", 503)
            normalized = "".join(hit.chunk.content.split())
            key = (hit.chunk.document_id, hit.chunk.excel_source.source_anchor, normalized) if hit.chunk.excel_source else normalized
            if not normalized or key in seen:
                continue
            seen.add(key)
            unique.append(hit)
            if len(unique) == self.top_k:
                break
        if self.context_expander is None or not unique:
            return unique
        try:
            expanded = self.context_expander(user_id, document_ids, unique)
        except AppError as error:
            if error.code not in {"MILVUS_UNAVAILABLE", "PARENT_CONTEXT_TIMEOUT"}:
                raise
            return [replace(hit, context_warning=error.code) for hit in unique]
        # Expansion can enrich, but cannot reorder, replace or add retrieved evidence.
        if len(expanded) != len(unique) or any(
            before.chunk != after.chunk or before.score != after.score
            or after.parent_context and not after.parent_context.contains(before.chunk)
            for before, after in zip(unique, expanded)
        ):
            raise AppError("VECTOR_SCOPE_MISMATCH", "父块上下文改变了实际命中的证据", 503)
        return expanded
