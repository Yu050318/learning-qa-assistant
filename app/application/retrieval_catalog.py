"""Build retrieval clients from frozen execution parameters and trusted index records."""
from app.application.retriever import RetrieverService
from app.application.hierarchy import HierarchyService
from app.core.errors import AppError
from app.domain.retrieval_scope import index_connection_identity
from app.infrastructure.embeddings.qwen import QwenEmbedding
from app.infrastructure.rerank import TextReranker
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


def build_retriever(settings, database, *, vectors=None, embeddings=None):
    vectors = vectors or MilvusVectorStore(settings)
    embeddings = embeddings or QwenEmbedding(settings)
    context_expander = vectors.expand_context if settings.retrieval_parent_context else None
    hierarchy = HierarchyService(settings, database, legacy_expander=context_expander) if settings.hierarchy_enabled else None

    def index_backend(target):
        if target.connection_sha256 != index_connection_identity(settings) or target.database_name != settings.milvus_database:
            raise AppError("DOCUMENT_SCOPE_CHANGED", "冻结索引连接不可用", 409)
        bound = settings.model_copy(update={"milvus_collection": target.collection_name,
            "embedding_model": target.embedding_model, "embedding_dimension": target.embedding_dimension,
            "rerank_enabled": False})  # The outer retriever reranks the pooled results once.
        return build_retriever(bound, database)

    return RetrieverService(embeddings, vectors, settings.retrieval_top_k, context_expander=context_expander,
        reranker=TextReranker(settings) if settings.rerank_enabled else None,
        hierarchy_expander=hierarchy.expand if hierarchy else None,
        index_factory=index_backend, settings_factory=lambda frozen: build_retriever(frozen, database))
