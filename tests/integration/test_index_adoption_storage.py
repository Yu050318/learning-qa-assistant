"""Opt-in, disposable PostgreSQL + Milvus adoption; no model providers involved."""
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application.ingestion import IngestionService
from app.core.config import Settings
from app.core.errors import AppError
from app.index_adoption import adopt
from app.infrastructure.loaders.local import LocalDocumentLoader
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import DocumentIndex, DocumentRecord, User
from app.infrastructure.vectorstores.milvus import MilvusVectorStore, user_filter
from app.migrate import upgrade
from .test_platform_migrations import database

pytestmark = pytest.mark.skipif(os.getenv("RUN_PLATFORM_MIGRATION_INTEGRATION") != "1"
    or os.getenv("RUN_INDEX_ADOPTION_STORAGE_INTEGRATION") != "1", reason="requires disposable PostgreSQL and Milvus databases")


@pytest.fixture
def stored(database, tmp_path, monkeypatch):
    # The explicit storage run and its CLI subprocess must use the same real
    # tokenizer; the repository-wide offline unit fixture intentionally stubs it.
    import tiktoken
    from tiktoken.registry import get_encoding
    monkeypatch.setattr(tiktoken, "get_encoding", get_encoding)
    upgrade(database)
    suffix = uuid4().hex[:12]
    settings = Settings(_env_file=None, database_url=database.engine.url.render_as_string(hide_password=False),
        database_require_tls=False, ingestion_mode="durable", document_versions_enabled=True,
        milvus_uri=os.getenv("MILVUS_ADOPTION_TEST_URI", "http://127.0.0.1:19530"),
        milvus_token=os.getenv("MILVUS_ADOPTION_TEST_TOKEN", ""), milvus_database="codex_stage6_adoption_test_" + suffix,
        milvus_collection="adoption_source_" + suffix, memory_collection="adoption_memory_" + suffix,
        embedding_model="adoption-fixture-model", embedding_dimension=64, chunk_size=100, chunk_overlap=10,
        upload_dir=tmp_path / "uploads", parsed_dir=tmp_path / "parsed", milvus_timeout=20)
    db = Database(settings)
    db.__dict__["engine"] = database.engine
    vectors = MilvusVectorStore(settings)
    loader = LocalDocumentLoader(settings)
    ingestion = IngestionService(settings, db, loader, None, None, vectors)
    uid, did = uuid4(), uuid4()
    content = ("# Original legacy source\nComplete source verification requires all fragments. " * 240).encode()
    path = settings.upload_dir / str(uid) / f"{did}.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    settings.parsed_dir.mkdir(parents=True)
    leaves = [replace(leaf, chunk_id=str(uuid4())) for leaf in loader.chunks(loader.load(path, ".md"), uid, did, "legacy.md")]
    vector = [1.0] + [0.0] * 63
    try:
        vectors.initialize()
        vectors.upsert(leaves, [vector for _ in leaves])
        with db.sessions() as session:
            session.add(User(id=uid, username="adoption-fixture", password_hash="fixture"))
            session.flush()
            session.add(DocumentRecord(id=did, user_id=uid, original_name="legacy.md", file_type=".md", file_path=str(path),
                content_hash=hashlib.sha256(content).hexdigest(), status="ready", chunk_count=len(leaves),
                embedding_model=settings.embedding_model, parser_metadata={"provider": "local", "generation": 1}))
            session.commit()
        yield SimpleNamespace(settings=settings, database=db, vectors=vectors, ingestion=ingestion,
            uid=uid, did=did, leaves=leaves, vector=vector, path=path, source=content)
    finally:
        ingestion.close()
        # The fixture owns this exact new namespace; never clean business collections.
        assert settings.milvus_database == "codex_stage6_adoption_test_" + suffix
        with vectors.connection() as client:
            for collection in (settings.milvus_collection, settings.memory_collection):
                if client.has_collection(collection):
                    client.drop_collection(collection)
        with vectors.connection(default_database=True) as client:
            client.drop_database(settings.milvus_database)


def vector_rows(s):
    with s.vectors.connection() as client:
        rows = client.query(s.vectors.collection, filter=user_filter(s.uid, [s.did]),
            output_fields=["chunk_id", "vector", "embedding_model"], limit=len(s.leaves) + 1, consistency_level="Strong")
    return sorted(rows, key=lambda row: row["chunk_id"])


def test_cli_preview_and_adoption_preserve_real_vector_values_and_source_bytes(stored):
    s = stored
    before = vector_rows(s)
    env = {**os.environ, "DATABASE_URL": s.settings.database_url.get_secret_value(), "DATABASE_REQUIRE_TLS": "false",
        "MILVUS_URI": s.settings.milvus_uri, "MILVUS_TOKEN": s.settings.milvus_token.get_secret_value(),
        "MILVUS_DATABASE": s.settings.milvus_database, "MILVUS_COLLECTION": s.settings.milvus_collection,
        "MEMORY_COLLECTION": s.settings.memory_collection, "MILVUS_TIMEOUT": "20", "EMBEDDING_MODEL": s.settings.embedding_model,
        "EMBEDDING_DIMENSION": "64", "CHUNK_SIZE": "100", "CHUNK_OVERLAP": "10", "UPLOAD_DIR": str(s.settings.upload_dir),
        "PARSED_DIR": str(s.settings.parsed_dir), "INGESTION_MODE": "durable", "DOCUMENT_VERSIONS_ENABLED": "true",
        "DOCUMENT_PUBLICATION_ENABLED": "false", "WORKSPACES_ENABLED": "false", "HIERARCHY_ENABLED": "false",
        "MODEL_CENTER_ENABLED": "false", "EVALUATION_ENABLED": "false", "CHAT_RUNS_ENABLED": "false",
        "DASHSCOPE_API_KEY": "", "DEEPSEEK_API_KEY": "", "MINERU_API_TOKEN": ""}
    result = subprocess.run([sys.executable, "-m", "app.index_adoption", "--user-id", str(s.uid), "--document-id", str(s.did)],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert result.returncode == 0, result.stdout
    preview = json.loads(result.stdout)
    assert preview["status"] == "verified" and not preview["applied"]
    assert preview["leaf_count"] == len(s.leaves) > 1
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, s.did) is None
    applied = adopt(s.ingestion, s.uid, s.did, apply=True, maintenance=True, expected_leaf_sha256=preview["leaf_sha256"])
    assert applied["applied"] and applied["state"] == "published"
    assert vector_rows(s) == before and s.path.read_bytes() == s.source
    assert adopt(s.ingestion, s.uid, s.did)["status"] == "already_bound"


def test_real_milvus_rejects_later_content_wrong_row_model_and_overflow(stored):
    s = stored
    last = s.leaves[-1]
    s.vectors.upsert([replace(last, content="corrupt last fragment")], [s.vector])
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.did)
    assert error.value.code == "INDEX_ADOPTION_CONTENT_MISMATCH"
    s.vectors.upsert([last], [s.vector])
    row = {**asdict(last), "vector": s.vector, "embedding_model": "different-model",
        "page_number": last.page_number or 0, "section": last.section or ""}
    with s.vectors.connection() as client:
        client.upsert(s.vectors.collection, data=[row])
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.did)
    assert error.value.code == "INDEX_VERIFICATION_FAILED"
    s.vectors.upsert([last], [s.vector])
    extra = replace(last, chunk_id=str(uuid4()), chunk_index=len(s.leaves))
    s.vectors.upsert([extra], [s.vector])
    with pytest.raises(AppError) as error:
        adopt(s.ingestion, s.uid, s.did)
    assert error.value.code == "HIERARCHY_STALE"
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, s.did) is None
    assert s.path.read_bytes() == s.source
