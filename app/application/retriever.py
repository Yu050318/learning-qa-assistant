from uuid import UUID

from app.core.errors import AppError
from app.domain.contracts import EmbeddingProvider, SearchHit, VectorStore


class RetrieverService:
    def __init__(self, embeddings: EmbeddingProvider, vectors: VectorStore, top_k: int):
        self.embeddings = embeddings
        self.vectors = vectors
        self.top_k = top_k

    def retrieve(self, user_id: UUID, query: str, document_ids: list[UUID]) -> list[SearchHit]:
        vector = self.embeddings.embed_query(query)
        hits = self.vectors.search(user_id, document_ids, vector, self.top_k * 2)
        allowed = {str(document_id) for document_id in document_ids}
        unique = []
        seen = set()
        for hit in hits:
            if hit.chunk.user_id != str(user_id) or hit.chunk.document_id not in allowed:
                raise AppError("VECTOR_SCOPE_MISMATCH", "检索结果不属于当前用户范围", 503)
            normalized = "".join(hit.chunk.content.split())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(hit)
            if len(unique) == self.top_k:
                break
        return unique
