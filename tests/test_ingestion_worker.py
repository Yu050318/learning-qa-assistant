from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.application.ingestion import IngestionService
from app.core.errors import AppError, UpstreamError
from app.infrastructure.persistence.models import IngestionJob
from app.worker import IngestionWorker
from test_document_versions import system


@pytest.fixture
def durable(system):
    system.ingestion.close()
    # The version fixture binds an externally opened Connection. End its setup
    # transaction so these tests exercise real Session commit/rollback ownership.
    system.database.sessions.kw["bind"].commit()
    system.settings.ingestion_mode = "durable"
    locks = set()

    @contextmanager
    def document_lock(document_id):
        if document_id in locks:
            raise AppError("RESOURCE_BUSY", "busy", 409)
        locks.add(document_id)
        try:
            yield lambda: None
        finally:
            locks.remove(document_id)

    system.database.document_lock = document_lock
    system.ingestion = IngestionService(system.settings, system.database, system.ingestion.loader,
                                        None, system.embeddings, system.vectors)
    system.queue = system.ingestion.queue
    system.clock = SimpleNamespace(now=datetime(2026, 10, 16, tzinfo=timezone.utc))
    system.queue.now = lambda session: system.clock.now
    system.worker = IngestionWorker(system.ingestion)
    yield system
    system.ingestion.close()


def upload(value, content="durable document"):
    return value.ingestion.upload(value.uid, "manual.md", "text/markdown", BytesIO(content.encode()))


def expire(value):
    value.clock.now += timedelta(seconds=value.settings.ingestion_lease_seconds + 1)


def test_upload_is_atomic_with_job_and_api_never_runs_it(durable):
    value = durable
    doc = upload(value)
    assert value.ingestion.executor is None
    assert value.ingestion.schedule(value.uid, doc.id)
    assert value.ingestion.get(value.uid, doc.id).status == "processing"
    assert value.queue.status(value.uid, doc.id)["state"] == "queued"
    with pytest.raises(AppError):
        value.queue.status(value.other, doc.id)
    assert value.worker.run_once()
    assert value.ingestion.get(value.uid, doc.id).status == "ready"
    assert value.queue.status(value.uid, doc.id)["state"] == "succeeded"
    assert not value.worker.run_once()


def test_enqueue_failure_rolls_back_document_version_and_file(durable):
    with patch.object(durable.queue, "enqueue", side_effect=RuntimeError("fixture")):
        with pytest.raises(RuntimeError):
            upload(durable)
    assert durable.ingestion.list(durable.uid, 0, 50) == []
    assert not list(durable.settings.upload_dir.rglob("*.md"))


def test_expiry_and_new_claim_fence_old_owner(durable):
    doc = upload(durable)
    first = durable.queue.claim()
    assert durable.queue.claim() is None
    durable.queue.heartbeat(first)
    expire(durable)
    with pytest.raises(AppError, match="租约"):
        durable.queue.heartbeat(first)
    second = durable.queue.claim()
    assert second.token != first.token
    for action in (durable.queue.assert_owned, durable.queue.heartbeat, durable.queue.release_busy, durable.queue.finish):
        with pytest.raises(AppError) as failure:
            action(first)
        assert failure.value.code == "INGESTION_LEASE_LOST"
    assert durable.queue.status(durable.uid, doc.id)["attempts"] == 2
    expire(durable)
    assert durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["state"] == "succeeded"


def test_repeated_crashes_stop_at_budget(durable):
    doc = upload(durable)
    for _ in range(durable.settings.ingestion_max_attempts):
        assert not durable.queue.claim().exhausted
        expire(durable)
    with patch.object(durable.ingestion, "process") as process:
        assert durable.worker.run_once()
        process.assert_not_called()
    status = durable.queue.status(durable.uid, doc.id)
    assert status["state"] == "failed" and status["last_error_code"] == "INGESTION_ATTEMPTS_EXHAUSTED"
    assert durable.ingestion.get(durable.uid, doc.id).status == "failed"
    assert not durable.worker.run_once()


@pytest.mark.parametrize("transient", [False, True])
def test_crash_after_failure_commit_preserves_retry_policy(durable, transient):
    doc = upload(durable)
    error = UpstreamError("embedding", retryable=True) if transient else AppError("MINERU_SUBMISSION_UNKNOWN", "uncertain")
    with patch.object(durable.embeddings, "embed_documents", side_effect=error), \
            patch.object(durable.queue, "finish", side_effect=RuntimeError("crash before ack")):
        durable.worker.run_once()
    assert durable.ingestion.get(durable.uid, doc.id).status == "failed"
    expire(durable)
    with patch.object(durable.embeddings, "embed_documents", wraps=durable.embeddings.embed_documents) as embed:
        durable.worker.run_once()
        assert embed.call_count == int(transient)
    status = durable.queue.status(durable.uid, doc.id)
    assert status["state"] == ("succeeded" if transient else "failed")
    if not transient:
        assert status["last_error_code"] == "MINERU_SUBMISSION_UNKNOWN"


def test_ready_before_ack_is_not_reprocessed_even_when_attempts_exhausted(durable):
    doc = upload(durable)
    for _ in range(durable.settings.ingestion_max_attempts):
        claim = durable.queue.claim()
        expire(durable)
    with durable.database.sessions() as session:
        from app.infrastructure.persistence.models import DocumentRecord
        session.get(DocumentRecord, doc.id).status = "ready"
        session.commit()
    with patch.object(durable.ingestion, "process") as process:
        durable.worker.run_once()
        process.assert_not_called()
    assert durable.queue.status(durable.uid, doc.id)["state"] == "succeeded"


def test_transient_retry_backoff_and_manual_reset(durable):
    doc = upload(durable)
    with patch.object(durable.embeddings, "embed_documents", side_effect=UpstreamError("embedding", retryable=True)):
        durable.worker.run_once()
    status = durable.queue.status(durable.uid, doc.id)
    assert status["state"] == "queued" and status["attempts"] == 1
    assert durable.ingestion.get(durable.uid, doc.id).parser_metadata["phase"] == "retry_wait"
    assert not durable.worker.run_once()
    durable.clock.now += timedelta(seconds=5)
    assert durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["state"] == "succeeded"


@pytest.mark.parametrize("error", [AppError("CONFIGURATION_ERROR", "missing"),
    AppError("MINERU_SUBMISSION_UNKNOWN", "ambiguous"), UpstreamError("embedding"), ValueError("private detail")])
def test_permanent_errors_do_not_retry_or_leak_private_details(durable, error):
    doc = upload(durable)
    with patch.object(durable.embeddings, "embed_documents", side_effect=error):
        durable.worker.run_once()
    status = durable.queue.status(durable.uid, doc.id)
    assert status["state"] == "failed" and status["attempts"] == 1
    assert "private detail" not in str(status)
    assert not durable.worker.run_once()
    durable.ingestion.retry(durable.uid, doc.id)
    assert durable.queue.status(durable.uid, doc.id)["attempts"] == 0
    assert durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["state"] == "succeeded"


def test_busy_document_does_not_consume_attempts_and_delete_cascades(durable):
    doc = upload(durable)
    with durable.ingestion.exclusive(doc.id):
        durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["attempts"] == 0
    durable.ingestion.delete(durable.uid, doc.id)
    with durable.database.sessions() as session:
        assert session.get(IngestionJob, doc.id) is None
    assert not durable.worker.run_once()


def test_lease_loss_after_embedding_prevents_upsert_failure_cleanup_and_ready(durable):
    doc = upload(durable)

    def embed(values):
        expire(durable)
        return [[0.] * 64 for _ in values]

    with patch.object(durable.embeddings, "embed_documents", side_effect=embed), \
            patch.object(durable.vectors, "upsert") as upsert, \
            patch.object(durable.vectors, "delete_document", wraps=durable.vectors.delete_document) as cleanup:
        durable.worker.run_once()
        upsert.assert_not_called()
        assert cleanup.call_count == 1
    assert durable.ingestion.get(durable.uid, doc.id).status == "processing"
    assert durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["state"] == "succeeded"


def test_reconcile_adopts_only_unqueued_processing_documents(durable):
    doc = upload(durable)
    with durable.database.sessions() as session:
        session.delete(session.get(IngestionJob, doc.id));session.commit()
    assert durable.ingestion.recover_pending() == 1
    assert durable.ingestion.recover_pending() == 0
    claim = durable.queue.claim()
    assert durable.ingestion.recover_pending() == 0
    durable.queue.assert_owned(claim)


def test_retry_exhaustion_and_source_hash_validation(durable):
    doc = upload(durable)
    with patch.object(durable.embeddings, "embed_documents", side_effect=UpstreamError("embedding", retryable=True)):
        for _ in range(durable.settings.ingestion_max_attempts):
            assert durable.worker.run_once()
            durable.clock.now += timedelta(seconds=300)
    assert durable.queue.status(durable.uid, doc.id)["state"] == "failed"
    assert not durable.worker.run_once()
    durable.ingestion.retry(durable.uid, doc.id)
    durable.ingestion.file_path(doc.file_path).write_text("changed", encoding="utf-8")
    durable.worker.run_once()
    assert durable.queue.status(durable.uid, doc.id)["last_error_code"] == "DOCUMENT_SOURCE_CHANGED"


def test_legacy_worker_rejected(system):
    with pytest.raises(AppError) as failure:
        IngestionWorker(system.ingestion)
    assert failure.value.code == "CONFIGURATION_ERROR"


def test_http_upload_processing_and_retry_share_existing_api(durable):
    from fastapi.testclient import TestClient
    from uuid import UUID
    from app.main import create_app
    from test_api_v2 import Services

    services = Services()
    services.current_user, services.settings, services.ingestion = durable.uid, durable.settings, durable.ingestion
    headers = {"Authorization": "Bearer fixture"}
    with TestClient(create_app(durable.settings, services)) as client:
        response = client.post("/api/v2/documents", headers=headers,
                               files={"file": ("worker.md", b"worker fixture", "text/markdown")})
        assert response.status_code == 202
        did = UUID(response.json()["id"])
        status = client.get(f"/api/v2/documents/{did}/processing", headers=headers).json()
        assert status["task"]["state"] == "queued"
        assert "claim_token" not in str(status) and "worker_id" not in str(status)
        services.current_user = durable.other
        assert client.get(f"/api/v2/documents/{did}/processing", headers=headers).status_code == 404
        assert client.post(f"/api/v2/documents/{did}/retry", headers=headers).status_code == 404
        services.current_user = durable.uid
        durable.worker.run_once()
        assert client.get(f"/api/v2/documents/{did}/processing", headers=headers).json()["task"]["state"] == "succeeded"


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (500, True), (503, True)])
def test_embedding_http_errors_classify_automatic_retry(status, retryable):
    import httpx
    from app.core.config import Settings
    from app.infrastructure.embeddings.qwen import QwenEmbedding

    request = httpx.Request("POST", "https://embedding.example/embeddings")
    error = httpx.HTTPStatusError("fixture", request=request, response=httpx.Response(status, request=request))
    with patch("app.infrastructure.embeddings.qwen.httpx.Client") as client:
        client.return_value.__enter__.return_value.post.return_value.raise_for_status.side_effect = error
        with pytest.raises(UpstreamError) as caught:
            QwenEmbedding(Settings(_env_file=None, dashscope_api_key="fixture")).embed_documents(["text"])
    assert caught.value.retryable is retryable
