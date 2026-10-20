"""An atomic logical retirement fans out to fenced, recoverable cleanup jobs."""
from io import BytesIO
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.infrastructure.persistence.models import ChatRun, ChatSession, DocumentIndex, DocumentSeries, DocumentTask, IngestionJob, RunIndexPin
from test_document_tasks import published, built
from test_ingestion_worker import durable
from test_document_versions import system


def family(s):
    first = built(s)
    second = s.ingestion.upload(s.uid, "v2.md", "text/markdown", BytesIO(b"second family revision"), base_document_id=first.id)
    assert s.worker.run_once()
    return first, second


def test_whole_family_closes_atomically_and_cleans_all_versions(published):
    s = published
    first, second = family(s)
    parent = s.ingestion.tasks.retire_series(s.uid, second.id)
    assert parent.kind == "delete_series" and parent.total_units == 2
    assert s.ingestion.tasks.retire_series(s.uid, first.id).id == parent.id
    with s.database.sessions() as session:
        series = session.get(DocumentSeries, first.id)
        assert series.retirement_task_id == parent.id and series.current_document_id is None
        children = session.scalars(select(DocumentTask).where(DocumentTask.parent_task_id == parent.id)).all()
        assert len(children) == 2 and all(child.kind == "delete" for child in children)
    assert all(s.ingestion.get(s.uid, doc.id).status == "deleting" for doc in (first, second))
    assert s.vectors.chunks  # The HTTP-side operation only revokes access and queues cleanup.
    with pytest.raises(AppError):
        s.ingestion.upload(s.uid, "v3.md", "text/markdown", BytesIO(b"closed family"), base_document_id=first.id)
    assert s.worker.run_once()
    interim = s.ingestion.tasks.detail(s.uid, parent.id)
    assert interim["processed_units"] == 1 and interim["state"] != "succeeded"
    assert s.worker.run_once()
    done = s.ingestion.tasks.detail(s.uid, parent.id)
    assert done["state"] == "succeeded" and done["processed_units"] == 2 and not done["can_retry"]
    assert not s.vectors.chunks
    assert all(s.ingestion.get(s.uid, doc.id).status == "retired" for doc in (first, second))
    with pytest.raises(AppError) as error:
        s.ingestion.upload(s.uid, "new.md", "text/markdown", BytesIO(b"new closed family revision"), base_document_id=second.id)
    assert error.value.code == "DOCUMENT_SERIES_RETIRED"


def test_unadopted_member_aborts_the_entire_family_without_revoking_other_versions(published):
    s = published
    first, second = family(s)
    with s.database.sessions() as session:
        session.delete(session.get(DocumentIndex, second.id))
        session.commit()
    with pytest.raises(AppError) as error:
        s.ingestion.tasks.retire_series(s.uid, first.id)
    assert error.value.code == "DOCUMENT_INDEX_NOT_ADOPTED"
    with s.database.sessions() as session:
        series = session.get(DocumentSeries, first.id)
        assert series.retirement_task_id is None and series.current_document_id == first.id
        assert not session.scalars(select(DocumentTask).where(DocumentTask.kind == "delete_series")).all()
        assert session.get(DocumentIndex, first.id).state == "published"
    assert all(s.ingestion.get(s.uid, doc.id).status == "ready" for doc in (first, second))


def test_family_enqueue_failure_rolls_back_every_receipt_and_version(published, monkeypatch):
    s = published
    first, second = family(s)
    original = s.queue.enqueue
    calls = []
    def enqueue(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("transaction fixture")
        return original(*args, **kwargs)
    monkeypatch.setattr(s.queue, "enqueue", enqueue)
    with pytest.raises(RuntimeError):
        s.ingestion.tasks.retire_series(s.uid, first.id)
    with s.database.sessions() as session:
        assert session.get(DocumentSeries, first.id).retirement_task_id is None
        assert not session.scalars(select(DocumentTask).where(DocumentTask.kind.in_(("delete", "delete_series")))).all()
    assert all(s.ingestion.get(s.uid, doc.id).status == "ready" for doc in (first, second))


def test_family_counts_already_retired_members_without_recleaning_them(published):
    s = published
    first, second = family(s)
    earlier = s.ingestion.delete(s.uid, first.id)
    assert s.worker.run_once()
    parent = s.ingestion.tasks.retire_series(s.uid, second.id)
    assert parent.processed_units == 1 and parent.total_units == 2
    children = s.ingestion.tasks.list(s.uid, parent_task_id=parent.id)
    assert children["total"] == 1 and children["items"][0]["document_id"] == second.id
    assert s.ingestion.tasks.detail(s.uid, earlier.id)["parent_task_id"] is None
    assert s.worker.run_once()
    assert s.ingestion.tasks.detail(s.uid, parent.id)["state"] == "succeeded"


def test_family_withdraws_all_shared_bindings_in_the_same_transaction(published):
    from app.application.sharing import SharingService
    from app.infrastructure.persistence.models import KnowledgeDocument
    s = published
    first, second = family(s)
    s.settings.sharing_enabled = True
    sharing = SharingService(s.settings, s.database)
    oid = sharing.create_organization(s.uid, "family team")["id"]
    kid = sharing.create_base(s.uid, oid, "family library")["id"]
    sharing.bind(s.uid, kid, first.id)
    s.ingestion.activate_version(s.uid, second.id, first.id)
    sharing.bind(s.uid, kid, second.id, expected_current=first.id)
    s.ingestion.tasks.retire_series(s.uid, first.id)
    assert sharing.documents(s.uid, kid) == []
    assert sharing.scope(s.uid, [kid], include_owned=False) == {}
    with s.database.sessions() as session:
        assert session.scalars(select(KnowledgeDocument)).all() == []


def test_failed_child_retry_updates_parent_without_double_counting_receipts(published, monkeypatch):
    s = published
    first, second = family(s)
    parent = s.ingestion.tasks.retire_series(s.uid, first.id)
    original = s.vectors.delete_document
    monkeypatch.setattr(s.vectors, "delete_document", lambda *args: (_ for _ in ()).throw(RuntimeError("fixture")))
    assert s.worker.run_once()
    monkeypatch.setattr(s.vectors, "delete_document", original)
    assert s.worker.run_once()
    summary = s.ingestion.tasks.detail(s.uid, parent.id)
    assert summary["state"] == "failed" and summary["processed_units"] == 1
    failed = next(row for row in s.ingestion.tasks.list(s.uid)["items"] if row["parent_task_id"] == parent.id and row["state"] == "failed")
    assert failed["can_retry"]
    child = s.ingestion.tasks.retry(s.uid, failed["id"])
    assert child.parent_task_id == parent.id
    assert s.ingestion.tasks.detail(s.uid, parent.id)["state"] != "failed"
    assert s.worker.run_once()
    summary = s.ingestion.tasks.detail(s.uid, parent.id)
    assert summary["state"] == "succeeded" and summary["processed_units"] == 2
    assert s.ingestion.tasks.detail(s.uid, failed["id"])["state"] == "failed"


def test_family_waits_for_pinned_version_and_preserves_other_version_progress(published):
    s = published
    first, second = family(s)
    with s.database.sessions() as session:
        chat = ChatSession(id=uuid4(), user_id=s.uid, title="Pinned family")
        session.add(chat)
        session.flush()
        run = ChatRun(id=uuid4(), user_id=s.uid, session_id=chat.id, client_key=uuid4(), fingerprint="a" * 64,
            state="waiting", request_id="family-pin", expires_at=s.clock.now + timedelta(hours=1))
        session.add(run)
        session.flush()
        session.add(RunIndexPin(run_id=run.id, document_id=first.id))
        session.commit()
        run_id = run.id
    parent = s.ingestion.tasks.retire_series(s.uid, first.id)
    assert s.worker.run_once() and s.worker.run_once()
    summary = s.ingestion.tasks.detail(s.uid, parent.id)
    assert (summary["state"], summary["phase"], summary["processed_units"]) == ("queued", "waiting_runs", 1)
    assert s.ingestion.file_path(first.file_path).exists() and not s.ingestion.file_path(second.file_path).exists()
    # A periodic probe must not make the parent flash back to "cleaning" while
    # its version is still pinned by the same waiting run.
    s.clock.now += timedelta(seconds=6)
    probe = s.queue.claim()
    assert s.ingestion.tasks.detail(s.uid, parent.id)["phase"] == "waiting_runs"
    s.queue.finish(probe, AppError("DOCUMENT_INDEX_PINNED", "fixture", 409))
    with s.database.sessions() as session:
        session.get(ChatRun, run_id).state = "cancelled"
        session.commit()
    s.clock.now += timedelta(seconds=6)
    assert s.worker.run_once()
    assert s.ingestion.tasks.detail(s.uid, parent.id)["state"] == "succeeded"


def test_family_http_is_owner_scoped_and_reports_parent_receipt(published):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api_v2 import Services
    s = published
    first, second = family(s)
    services = Services()
    services.current_user, services.settings, services.ingestion = s.other, s.settings, s.ingestion
    headers = {"Authorization": "Bearer fixture"}
    url = f"/api/v2/documents/{first.id}/retire-series"
    body = {"expected_version_count": 2}
    with TestClient(create_app(s.settings, services)) as client:
        assert client.post(url, headers=headers, json=body).status_code == 404
        services.current_user = s.uid
        assert client.post(url, headers=headers, json={"expected_version_count": 1}).status_code == 409
        assert client.post(url, headers=headers, json={}).status_code == 422
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["kind"] == "delete_series" and receipt["total_units"] == 2
        assert receipt["parent_task_id"] is None and "details" not in receipt
        assert client.post(url, headers=headers, json=body).json()["id"] == receipt["id"]
        assert client.get("/api/v2/document-tasks?kind=delete_series", headers=headers).json()["total"] == 1
        children = client.get(f"/api/v2/document-tasks?parent_task_id={receipt['id']}", headers=headers).json()
        assert children["total"] == 2 and all(row["parent_task_id"] == receipt["id"] for row in children["items"])
        services.current_user = s.other
        assert client.get(f"/api/v2/document-tasks?parent_task_id={receipt['id']}", headers=headers).status_code == 404
