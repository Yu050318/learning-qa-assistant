"""Deployment checks must not invent bindings or rewrite frozen execution inputs."""
import json
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from app.application.preflight import deployment_preflight
from app.infrastructure.persistence.models import ChatRun, ChatSession, DocumentIndex, EvaluationDataset, EvaluationJob, utcnow
from test_document_tasks import published, built
from test_document_versions import system
from test_ingestion_worker import durable


def setup(s):
    s.settings.upload_dir.mkdir(parents=True, exist_ok=True)
    s.settings.parsed_dir.mkdir(parents=True, exist_ok=True)
    s.settings.jwt_secret = SecretStr("x" * 32)
    s.settings.dashscope_api_key = s.settings.deepseek_api_key = SecretStr("test-only-not-a-provider-key")
    s.database.check = lambda: None
    s.database.engine = MagicMock()
    s.database.engine.connect.return_value.__enter__.return_value.scalar.return_value = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    return SimpleNamespace(check=Mock(), for_collection=Mock(return_value=SimpleNamespace(check=Mock())))


def report(s, vectors):
    result = deployment_preflight(s.settings, s.database, vectors, require_worker=False)
    return result, {row["name"]: row for row in result["checks"]}


def payload(s, doc):
    with s.database.sessions() as session:
        target = s.ingestion.publication.freeze(session, {doc.id: s.uid})[0]
    return json.loads(json.dumps(asdict(target), default=str))


def test_verified_candidates_and_retired_tombstones_do_not_block_deployment(published):
    s = published
    doc = built(s)
    vectors = setup(s)
    assert report(s, vectors)[0]["ready"]
    s.ingestion.delete(s.uid, doc.id)
    assert s.worker.run_once()
    assert not s.ingestion.file_path(doc.file_path).exists()
    result, checks = report(s, vectors)
    assert result["ready"] and checks["document_files"]["status"] == "pass"
    s.settings.document_publication_enabled = False
    assert report(s, vectors)[0]["ready"]  # Fully cleaned tombstones remain inert.


@pytest.mark.parametrize("change,code", [("missing", "DOCUMENT_INDEX_NOT_ADOPTED"),
    ("count", "DOCUMENT_INDEX_NOT_VERIFIED"), ("connection", "DOCUMENT_INDEX_UNAVAILABLE")])
def test_preflight_rejects_missing_or_inconsistent_bindings_without_repairing_them(published, change, code):
    s = published
    doc = built(s)
    vectors = setup(s)
    with s.database.sessions() as session:
        row = session.get(DocumentIndex, doc.id)
        if change == "missing":
            session.delete(row)
        elif change == "count":
            row.leaf_count += 1
        else:
            row.connection_sha256 = "f" * 64
        session.commit()
    result, checks = report(s, vectors)
    assert not result["ready"] and checks["document_publication"]["code"] == code
    with s.database.sessions() as session:
        assert (session.get(DocumentIndex, doc.id) is None) == (change == "missing")


def test_source_hash_drift_and_disabling_bound_retrieval_are_reported(published):
    s = published
    doc = built(s)
    vectors = setup(s)
    s.ingestion.file_path(doc.file_path).write_text("changed source", encoding="utf-8")
    assert report(s, vectors)[1]["document_publication"]["code"] == "DOCUMENT_SOURCE_CHANGED"
    s.settings.document_publication_enabled = False
    assert report(s, vectors)[1]["document_publication"]["code"] == "BOUND_INDEX_FEATURE_DISABLED"


def test_preflight_checks_original_bound_collection_after_default_model_rotation(published):
    s = published
    doc = built(s)
    vectors = setup(s)
    old_collection, old_model, old_dimension = s.settings.milvus_collection, s.settings.embedding_model, s.settings.embedding_dimension
    s.settings.milvus_collection = "new_default"
    s.settings.embedding_model = "future-model"
    s.settings.embedding_dimension = 128
    assert report(s, vectors)[0]["ready"]
    vectors.for_collection.assert_called_once_with(old_collection, embedding_model=old_model, embedding_dimension=old_dimension)


@pytest.mark.parametrize("state", ["queued", "running", "waiting", "cancelling"])
def test_old_active_run_blocks_switch_without_rewriting_its_snapshot(published, state):
    s = published
    doc = built(s)
    vectors = setup(s)
    snapshot = {"context": {"allowed_document_ids": [str(doc.id)], "retrieval_indexes": []}}
    with s.database.sessions() as session:
        chat = ChatSession(id=uuid4(), user_id=s.uid, title="legacy snapshot")
        session.add(chat)
        session.flush()
        run = ChatRun(id=uuid4(), user_id=s.uid, session_id=chat.id, client_key=uuid4(), fingerprint="a" * 64,
            state=state, request_id="preflight-test", execution_snapshot=snapshot, expires_at=utcnow() + timedelta(hours=1))
        session.add(run)
        session.commit()
        rid = run.id
    result, checks = report(s, vectors)
    assert not result["ready"] and checks["frozen_runs"]["code"] == "LEGACY_FROZEN_SCOPE_REQUIRES_SETTLEMENT"
    with s.database.sessions() as session:
        row = session.get(ChatRun, rid)
        assert row.execution_snapshot == snapshot
        row.state = "cancelled"
        session.commit()
    assert report(s, vectors)[0]["ready"]


def test_complete_frozen_run_is_accepted_and_partial_coverage_is_rejected(published):
    s = published
    doc = built(s)
    vectors = setup(s)
    target = payload(s, doc)
    with s.database.sessions() as session:
        chat = ChatSession(id=uuid4(), user_id=s.uid, title="typed snapshot")
        session.add(chat)
        session.flush()
        run = ChatRun(id=uuid4(), user_id=s.uid, session_id=chat.id, client_key=uuid4(), fingerprint="a" * 64,
            state="waiting", request_id="preflight-test", expires_at=utcnow() + timedelta(hours=1),
            execution_snapshot={"context": {"allowed_document_ids": [str(doc.id)], "retrieval_indexes": [target]}})
        session.add(run)
        session.commit()
        rid = run.id
    assert report(s, vectors)[0]["ready"]
    with s.database.sessions() as session:
        row = session.get(ChatRun, rid)
        row.execution_snapshot = {"context": {"allowed_document_ids": [str(doc.id), str(uuid4())], "retrieval_indexes": [target]}}
        session.commit()
    assert not report(s, vectors)[0]["ready"]


def test_old_queued_evaluation_is_checked_even_if_its_worker_is_disabled(published):
    s = published
    doc = built(s)
    vectors = setup(s)
    with s.database.sessions() as session:
        dataset = EvaluationDataset(created_by=s.uid, name="scope fixture", fingerprint="a" * 64, case_count=1,
            content={"name": "scope fixture", "cases": [{"id": "missing", "question": "unknown?", "category": "missing", "expected_kinds": ["insufficient"], "split": "dev"}]})
        session.add(dataset)
        session.flush()
        job = EvaluationJob(created_by=s.uid, dataset_id=dataset.id, client_key=uuid4(), fingerprint="b" * 64,
            dataset_fingerprint=dataset.fingerprint, split="dev", profile="current", model_snapshot={},
            scope_snapshot={"document_ids": [str(doc.id)]}, total_cases=1)
        session.add(job)
        session.commit()
        jid = job.id
    assert not s.settings.evaluation_enabled
    assert report(s, vectors)[1]["frozen_evaluations"]["status"] == "fail"
    target = payload(s, doc)
    with s.database.sessions() as session:
        session.get(EvaluationJob, jid).scope_snapshot = {"document_ids": [str(doc.id)], "indexes": [target]}
        session.commit()
    assert report(s, vectors)[0]["ready"]


def test_unselected_shared_history_is_reported_without_automatically_publishing_it(published):
    from io import BytesIO
    from sqlalchemy import select
    from app.application.sharing import SharingService
    from app.infrastructure.persistence.models import KnowledgeDocument
    s = published
    first = built(s)
    s.settings.sharing_enabled = True
    sharing = SharingService(s.settings, s.database)
    oid = sharing.create_organization(s.uid, "preflight team")["id"]
    kid = sharing.create_base(s.uid, oid, "preflight history")["id"]
    sharing.bind(s.uid, kid, first.id)
    current = first.id
    for number in (2, 3):
        doc = s.ingestion.upload(s.uid, f"version-{number}.md", "text/markdown", BytesIO(f"version {number}".encode()), base_document_id=first.id)
        assert s.worker.run_once()
        s.ingestion.activate_version(s.uid, doc.id, current)
        sharing.bind(s.uid, kid, doc.id, expected_current=current)
        current = doc.id
    sharing.bind(s.uid, kid, current, remove=True)
    vectors = setup(s)
    result, checks = report(s, vectors)
    assert result["ready"] and checks["shared_version_selection"]["code"] == "SHARED_VERSION_SELECTION_REQUIRED"
    assert sharing.scope(s.uid, [kid], include_owned=False) == {}
    with s.database.sessions() as session:
        rows = session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kid)).all()
        assert len(rows) == 2 and all(not row.is_current for row in rows)
