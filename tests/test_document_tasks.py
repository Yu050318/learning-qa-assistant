"""Retirement runs through the same durable claim/fencing path as ingestion."""
from datetime import timedelta
from uuid import uuid4

import pytest

from app.application.document_publication import DocumentPublication
from app.core.errors import AppError
from app.infrastructure.persistence.models import ChatRun, ChatSession, DocumentIndex, DocumentSeries, RunIndexPin
from test_document_versions import system
from test_ingestion_worker import durable, upload


@pytest.fixture
def published(durable, monkeypatch):
    s = durable
    s.settings.document_publication_enabled = True
    s.ingestion.publication = DocumentPublication(s.settings, s.vectors)
    monkeypatch.setattr(s.vectors, "read_document_chunks", lambda uid, did, count, **kwargs:
        [chunk for chunk in s.vectors.chunks if chunk.user_id == str(uid) and chunk.document_id == str(did)], raising=False)
    return s


def built(s):
    doc = upload(s)
    assert s.worker.run_once()
    assert s.ingestion.get(s.uid, doc.id).status == "ready"
    return doc


def test_retirement_is_idempotent_and_preserves_task_history(published):
    s = published
    doc = built(s)
    task = s.ingestion.delete(s.uid, doc.id)
    assert s.ingestion.delete(s.uid, doc.id).id == task.id
    assert s.ingestion.get(s.uid, doc.id).status == "deleting"
    assert s.vectors.chunks and s.ingestion.file_path(doc.file_path).is_file()
    with s.database.sessions() as session:
        assert session.get(DocumentIndex, doc.id).state == "retiring"
        assert session.get(DocumentSeries, doc.id).current_document_id is None
    assert s.worker.run_once()
    assert s.ingestion.get(s.uid, doc.id).status == "retired"
    assert s.ingestion.list(s.uid, 0, 50) == []
    assert not s.vectors.chunks and not s.ingestion.file_path(doc.file_path).exists()
    assert s.ingestion.tasks.detail(s.uid, task.id)["state"] == "succeeded"
    assert s.ingestion.delete(s.uid, doc.id).id == task.id
    history = s.ingestion.tasks.list(s.uid)
    assert history["total"] == 2 and {item["kind"] for item in history["items"]} == {"ingest", "delete"}
    # The tombstone preserves history while allowing a new source upload.
    assert upload(s).id != doc.id


def test_failed_cleanup_can_retry_without_losing_the_failed_receipt(published, monkeypatch):
    s = published
    doc = built(s)
    task = s.ingestion.delete(s.uid, doc.id)
    original = s.vectors.delete_document
    monkeypatch.setattr(s.vectors, "delete_document", lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert s.worker.run_once()
    assert s.ingestion.tasks.detail(s.uid, task.id)["state"] == "failed"
    assert s.ingestion.tasks.detail(s.uid, task.id)["can_retry"]
    assert s.ingestion.get(s.uid, doc.id).status == "deleting"
    monkeypatch.setattr(s.vectors, "delete_document", original)
    retry = s.ingestion.tasks.retry(s.uid, task.id)
    assert retry.id != task.id
    assert s.ingestion.tasks.retry(s.uid, task.id).id == retry.id
    assert not s.ingestion.tasks.detail(s.uid, task.id)["can_retry"]
    assert s.worker.run_once()
    assert s.ingestion.tasks.detail(s.uid, retry.id)["state"] == "succeeded"
    assert s.ingestion.tasks.detail(s.uid, task.id)["state"] == "failed"


def test_ingestion_task_retry_is_idempotent_and_old_receipts_cannot_replace_later_actions(published, monkeypatch):
    from app.infrastructure.persistence.models import IngestionJob
    s = published
    doc = upload(s, "one failed upload")
    original = s.embeddings.embed_documents
    monkeypatch.setattr(s.embeddings, "embed_documents", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture")))
    assert s.worker.run_once()
    task = s.ingestion.tasks.list(s.uid)["items"][0]
    assert task["state"] == "failed" and task["can_retry"]
    monkeypatch.setattr(s.embeddings, "embed_documents", original)
    retry = s.ingestion.tasks.retry(s.uid, task["id"])
    assert s.ingestion.tasks.retry(s.uid, task["id"]).id == retry.id
    assert s.worker.run_once()
    assert s.ingestion.tasks.retry(s.uid, task["id"]).id == retry.id
    assert s.ingestion.tasks.detail(s.uid, retry.id)["state"] == "succeeded"
    retirement = s.ingestion.delete(s.uid, doc.id)
    with pytest.raises(AppError) as failure:
        s.ingestion.tasks.retry(s.uid, task["id"])
    assert failure.value.code == "DOCUMENT_TASK_NOT_RETRYABLE"
    with s.database.sessions() as session:
        assert session.get(IngestionJob, doc.id).task_id == retirement.id


def test_task_filters_and_pagination_preserve_owner_scope(published):
    s = published
    first = built(s)
    second = upload(s, "second source")
    assert s.ingestion.tasks.list(s.uid, task_state="queued")["total"] == 1
    assert s.ingestion.tasks.list(s.uid, task_state="succeeded")["total"] == 1
    assert s.ingestion.tasks.list(s.uid, kind="delete")["total"] == 0
    assert s.ingestion.tasks.list(s.uid, search="manual.md")["total"] == 2
    assert {row["document_id"] for row in s.ingestion.tasks.list(s.uid)["items"]} == {first.id, second.id}
    assert s.ingestion.tasks.list(s.uid, search="%")["total"] == 0
    assert len(s.ingestion.tasks.list(s.uid, limit=1)["items"]) == 1
    assert s.ingestion.tasks.list(s.other, search="manual")["total"] == 0


def test_task_history_and_retry_are_owner_scoped(published):
    s = published
    doc = built(s)
    task = s.ingestion.delete(s.uid, doc.id)
    assert s.ingestion.tasks.list(s.other)["total"] == 0
    for action in (lambda: s.ingestion.tasks.detail(s.other, task.id),
                   lambda: s.ingestion.tasks.retry(s.other, task.id),
                   lambda: s.ingestion.delete(s.other, doc.id)):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.status_code == 404


def test_default_index_change_cleans_the_original_binding(published, monkeypatch):
    s = published
    doc = built(s)
    task = s.ingestion.delete(s.uid, doc.id)
    collection = s.settings.milvus_collection
    s.settings.milvus_collection = "new_default"
    bound = []
    def for_collection(name, **kwargs):
        bound.append((name, kwargs))
        return s.vectors
    monkeypatch.setattr(s.vectors, "for_collection", for_collection, raising=False)
    assert s.worker.run_once()
    assert bound[0][0] == collection
    assert s.ingestion.tasks.detail(s.uid, task.id)["state"] == "succeeded"


def test_changed_connection_refuses_cleanup(published):
    s = published
    doc = built(s)
    task = s.ingestion.delete(s.uid, doc.id)
    s.settings.milvus_uri = "http://different-index:19530"
    assert s.worker.run_once()
    result = s.ingestion.tasks.detail(s.uid, task.id)
    assert result["state"] == "failed" and result["error_code"] == "DOCUMENT_INDEX_UNAVAILABLE"
    assert s.vectors.chunks and s.ingestion.file_path(doc.file_path).exists()


@pytest.mark.parametrize("state", ["queued", "running", "waiting", "cancelling"])
def test_active_run_pins_wait_without_spending_retry_budget(published, state):
    s = published
    doc = built(s)
    with s.database.sessions() as session:
        chat = ChatSession(id=uuid4(), user_id=s.uid, title="Pinned version")
        session.add(chat)
        session.flush()
        run = ChatRun(id=uuid4(), user_id=s.uid, session_id=chat.id, client_key=uuid4(),
            fingerprint="a" * 64, state=state, request_id="retirement-test",
            expires_at=s.clock.now + timedelta(hours=1))
        session.add(run)
        session.flush()
        session.add(RunIndexPin(run_id=run.id, document_id=doc.id))
        session.commit()
        run_id = run.id
    task = s.ingestion.delete(s.uid, doc.id)
    assert s.worker.run_once()
    result = s.ingestion.tasks.detail(s.uid, task.id)
    assert (result["state"], result["phase"], result["attempts"]) == ("queued", "waiting_runs", 0)
    assert s.vectors.chunks and s.ingestion.file_path(doc.file_path).exists()
    with s.database.sessions() as session:
        session.get(ChatRun, run_id).state = "cancelled"
        session.commit()
    s.clock.now += timedelta(seconds=6)
    assert s.worker.run_once()
    assert s.ingestion.tasks.detail(s.uid, task.id)["state"] == "succeeded"
    assert not s.vectors.chunks


def test_http_retirement_returns_a_task_and_keeps_owner_isolation(published):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api_v2 import Services
    s = published
    doc = built(s)
    services = Services()
    services.current_user, services.settings, services.ingestion = s.uid, s.settings, s.ingestion
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(create_app(s.settings, services)) as client:
        response = client.delete(f"/api/v2/documents/{doc.id}", headers=headers)
        assert response.status_code == 202
        task = response.json()
        assert task["kind"] == "delete" and task["state"] == "queued"
        assert "details" not in task and "connection_sha256" not in str(task)
        url = f"/api/v2/document-tasks/{task['id']}"
        assert client.get(url, headers=headers).status_code == 200
        assert client.get("/api/v2/document-tasks?limit=0", headers=headers).status_code == 422
        assert client.get("/api/v2/document-tasks", headers=headers).json()["total"] == 2
        for query in ("state=unknown", "kind=unknown", "offset=-1", "q=" + "x" * 201):
            assert client.get("/api/v2/document-tasks?" + query, headers=headers).status_code == 422
        filtered = client.get("/api/v2/document-tasks?state=queued&kind=delete&limit=1", headers=headers).json()
        assert filtered["total"] == 1 and len(filtered["items"]) == 1
        assert filtered["items"][0]["id"] == task["id"]
        assert filtered["items"][0]["can_retry"] is False
        assert client.get("/api/v2/document-tasks?offset=2&limit=1", headers=headers).json() == {"total": 2, "items": []}
        assert client.get("/api/v2/document-tasks?q=%25", headers=headers).json()["total"] == 0
        assert client.post(url + "/retry", headers=headers).status_code == 409
        services.current_user = s.other
        assert client.get(url, headers=headers).status_code == 404
        assert client.post(url + "/retry", headers=headers).status_code == 404
        assert client.get("/api/v2/document-tasks", headers=headers).json()["total"] == 0
        services.current_user = s.uid
        assert s.worker.run_once()
        assert client.get(url, headers=headers).json()["state"] == "succeeded"


def test_http_retry_returns_one_new_receipt_and_retains_failed_history(published, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api_v2 import Services
    s = published
    doc = built(s)
    parent = s.ingestion.delete(s.uid, doc.id)
    original = s.vectors.delete_document
    monkeypatch.setattr(s.vectors, "delete_document", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture")))
    assert s.worker.run_once()
    monkeypatch.setattr(s.vectors, "delete_document", original)
    services = Services()
    services.current_user, services.settings, services.ingestion = s.uid, s.settings, s.ingestion
    headers = {"Authorization": "Bearer fixture"}
    url = f"/api/v2/document-tasks/{parent.id}"
    with TestClient(create_app(s.settings, services)) as client:
        assert client.get(url, headers=headers).json()["can_retry"] is True
        response = client.post(url + "/retry", headers=headers)
        assert response.status_code == 202
        child = response.json()
        assert child["id"] != str(parent.id) and child["state"] == "queued"
        assert child["can_retry"] is False and "details" not in child
        assert client.post(url + "/retry", headers=headers).json()["id"] == child["id"]
        assert s.worker.run_once()
        assert client.post(url + "/retry", headers=headers).json()["state"] == "succeeded"
        failed = client.get(url, headers=headers).json()
        assert failed["state"] == "failed" and failed["can_retry"] is False
        history = client.get("/api/v2/document-tasks?kind=delete", headers=headers).json()
        assert history["total"] == 2


def test_frozen_index_rejects_in_place_hierarchy_rebuild_even_if_feature_disabled(published, monkeypatch):
    from app.hierarchy_backfill import backfill, enqueue
    s = published
    doc = built(s)
    s.settings.hierarchy_enabled = True
    s.settings.document_publication_enabled = False
    monkeypatch.setattr(s.vectors, "read_document_chunks", lambda *args, **kwargs:
        pytest.fail("immutable versions must be rejected before reading vectors"))
    for action in (lambda: enqueue(s.settings, s.database, s.uid, doc.id),
                   lambda: backfill(s.settings, s.database, s.vectors, s.uid, doc.id)):
        with pytest.raises(AppError) as failure:
            action()
        assert failure.value.code == "DOCUMENT_INDEX_IMMUTABLE"
