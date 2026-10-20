from dataclasses import asdict, FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest

from app.application.retriever import RetrieverService
from app.application.retrieval_catalog import build_retriever
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, SearchHit
from app.domain.retrieval_scope import RetrievalIndex, index_connection_identity


def target(uid, did, collection="previous_index", *, model="old-embedding", dimension=64, connection="c" * 64):
    return RetrievalIndex(did, uid, collection, "rag_tutorial", model, dimension, connection,
                          "b" * 64, "a" * 64, "d" * 64, 1)


def test_frozen_index_roundtrip_is_immutable_and_rejects_raw_filters_or_bad_targets():
    index = target(uuid4(), uuid4())
    payload = json.loads(json.dumps(asdict(index), default=str))
    assert RetrievalIndex.from_payload(payload) == index
    with pytest.raises(FrozenInstanceError):
        index.collection_name = "other"
    for altered in ({**payload, "filter_expr": "true"}, {**payload, "collection_name": "other or true"},
                    {**payload, "embedding_dimension": True}, {**payload, "leaf_count": 0}):
        with pytest.raises(ValueError):
            RetrievalIndex.from_payload(altered)


def test_retriever_clients_have_explicit_lifetimes_and_index_groups_do_not_rerank():
    settings = Settings(_env_file=None, rerank_enabled=True, rerank_api_key="isolated-test-no-provider", rerank_endpoint="https://rerank.example.invalid/rank")
    retriever = build_retriever(settings, SimpleNamespace())
    frozen = retriever.for_settings(settings.model_copy())
    index = target(uuid4(), uuid4(), connection=index_connection_identity(settings))
    bound = retriever.index_factory(index)
    assert bound.reranker is None
    assert frozen.reranker is not retriever.reranker
    frozen.close()
    assert frozen.reranker.client.is_closed and not retriever.reranker.client.is_closed
    retriever.close()
    assert retriever.reranker.client.is_closed


def test_container_close_owns_the_actual_retriever_after_factory_refactor():
    from app.application.container import Services
    services = Services(Settings(_env_file=None, jwt_secret="isolated-test-jwt-secret-32-bytes-only", rerank_enabled=True,
        rerank_api_key="isolated-test-no-provider", rerank_endpoint="https://rerank.example.invalid/rank"))
    client = services.retriever.reranker.client
    services.close()
    assert client.is_closed


def test_bound_backend_uses_stored_collection_and_embedding_without_mutating_defaults():
    settings = Settings(_env_file=None, milvus_collection="new_default", embedding_model="new-embedding", embedding_dimension=1024)
    index = target(uuid4(), uuid4(), connection=index_connection_identity(settings))
    retriever = build_retriever(settings, SimpleNamespace())
    bound = retriever.index_factory(index)
    assert bound.vectors.collection == "previous_index"
    assert bound.embeddings.settings.embedding_model == "old-embedding"
    assert bound.embeddings.settings.embedding_dimension == 64
    assert settings.milvus_collection == "new_default" and settings.embedding_dimension == 1024
    with pytest.raises(AppError) as error:
        retriever.index_factory(replace(index, connection_sha256="f" * 64))
    assert error.value.code == "DOCUMENT_SCOPE_CHANGED"


def test_multi_index_retrieval_pools_before_reranking_and_expands_in_correct_collection():
    uid, other = uuid4(), uuid4()
    indexes = [target(uid, uuid4(), "one"), target(uid, uuid4(), "two"),
               target(other, uuid4(), "three", model="second-embedding", dimension=128)]
    searches, embedded, expanded, reranked = [], [], [], []
    def factory(index):
        def embed(query):
            embedded.append((index.embedding_model, index.embedding_dimension, query))
            return [1.] * index.embedding_dimension
        def search(owner, ids, query, vector, count):
            assert owner == index.user_id and ids == [index.document_id] and len(vector) == index.embedding_dimension
            searches.append((index.collection_name, owner, tuple(ids)))
            score = {"one": .01, "two": .03, "three": .02}[index.collection_name]
            return [SearchHit(Chunk(str(uuid4()), str(owner), str(ids[0]), 0, index.collection_name, "scope.txt"), score)]
        def expand(owner, ids, hits):
            assert all(hit.chunk.document_id in {str(did) for did in ids} for hit in hits)
            expanded.append(index.collection_name)
            return [replace(hit, context_warning="bound_" + index.collection_name) for hit in hits]
        return RetrieverService(SimpleNamespace(embed_query=embed), SimpleNamespace(search=search), 2, context_expander=expand)
    def rerank(query, hits, count, **kwargs):
        reranked.append([hit.chunk.content for hit in hits])
        return hits[:count]
    root = RetrieverService(SimpleNamespace(), SimpleNamespace(), 2, index_factory=factory,
                            reranker=SimpleNamespace(rerank=rerank))
    hits = root.retrieve(uid, "question", [index.document_id for index in indexes], indexes=tuple(indexes),
                          document_owners={index.document_id: index.user_id for index in indexes})
    assert [hit.chunk.content for hit in hits] == ["two", "three"]
    assert [hit.context_warning for hit in hits] == ["bound_two", "bound_three"]
    assert reranked == [["two", "three", "one"]]
    assert [name for name, _, _ in searches] == ["one", "two", "three"]
    assert len(embedded) == 2 and expanded == ["two", "three"]


def test_incomplete_or_foreign_index_scope_is_rejected_before_embedding():
    uid, did = uuid4(), uuid4()
    called = []
    def factory(index):
        called.append(index)
        raise AssertionError("must not build a backend")
    retriever = RetrieverService(SimpleNamespace(), SimpleNamespace(), 2, index_factory=factory)
    for indexes in ((target(uid, uuid4()),), (target(uuid4(), did),)):
        with pytest.raises(AppError) as error:
            retriever.retrieve(uid, "question", [did], indexes=indexes)
        assert error.value.code == "VECTOR_SCOPE_MISMATCH"
    assert not called


def test_bound_index_cannot_return_evidence_from_another_version():
    uid, did = uuid4(), uuid4()
    rogue = Chunk(str(uuid4()), str(uid), str(uuid4()), 0, "must not escape", "other.txt")
    backend = RetrieverService(SimpleNamespace(embed_query=lambda query: [1.] * 64),
                               SimpleNamespace(search=lambda *args: [SearchHit(rogue, .1)]), 2)
    retriever = RetrieverService(SimpleNamespace(), SimpleNamespace(), 2, index_factory=lambda index: backend)
    with pytest.raises(AppError) as error:
        retriever.retrieve(uid, "question", [did], indexes=(target(uid, did),))
    assert error.value.code == "VECTOR_SCOPE_MISMATCH"
