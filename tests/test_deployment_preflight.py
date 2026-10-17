from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr

from app.application.preflight import deployment_preflight
from app.application.model_center import ModelCenter
from app.application.workers import WorkerRegistry
from app.core.errors import UpstreamError
from app.hierarchy_backfill import enqueue
from app.infrastructure.persistence.models import DocumentRecord, WorkerHeartbeat
from test_ingestion_worker import durable, system, upload


def test_third_party_dotenv_autoload_does_not_override_isolated_settings(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    from app.core.config import Settings
    path = tmp_path / "feature.env"
    path.write_text("MODEL_CENTER_ENABLED=true\n", encoding="utf-8")
    monkeypatch.delenv("MODEL_CENTER_ENABLED", raising=False)
    assert not load_dotenv(path)
    assert not Settings(_env_file=None).model_center_enabled
    assert Settings(_env_file=path).model_center_enabled


def test_worker_liveness_requires_matching_configuration_and_recent_heartbeat(durable):
    s = durable
    registry = WorkerRegistry(s.settings, s.database)
    assert registry.healthy_count() == 0
    registry.beat()
    assert registry.healthy_count(registry.worker_id) == 1
    old_path = s.settings.upload_dir
    s.settings.upload_dir = old_path / "different-volume"
    assert registry.healthy_count() == 0
    s.settings.upload_dir = old_path
    with s.database.sessions() as session:
        row = session.get(WorkerHeartbeat, registry.worker_id)
        row.seen_at = registry.now(session) - timedelta(seconds=46)
        session.commit()
    assert registry.healthy_count() == 0
    registry.beat()
    assert registry.healthy_count() == 1
    registry.stop()
    assert registry.healthy_count() == 0


def test_preflight_is_read_only_requires_worker_and_never_returns_raw_errors(durable):
    s = durable
    s.settings.upload_dir.mkdir(parents=True)
    s.settings.parsed_dir.mkdir(parents=True)
    s.settings.jwt_secret = SecretStr("x" * 32)
    s.settings.dashscope_api_key = s.settings.deepseek_api_key = SecretStr("test-only-private-key")
    s.settings.model_center_enabled = True
    ModelCenter(s.settings, s.database).initialize()
    s.database.check = lambda: None
    s.database.engine = MagicMock()
    s.database.engine.connect.return_value.__enter__.return_value.scalar.return_value = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    vectors = SimpleNamespace(check=lambda: None)
    report = deployment_preflight(s.settings, s.database, vectors)
    assert not report["ready"] and any(row["code"] == "WORKER_NOT_READY" for row in report["checks"])
    startup = deployment_preflight(s.settings, s.database, vectors, require_worker=False)
    assert startup["ready"], [row for row in startup["checks"] if row["status"] == "fail"]
    registry = WorkerRegistry(s.settings, s.database)
    registry.beat()
    assert deployment_preflight(s.settings, s.database, vectors)["ready"]
    with patch.object(vectors, "check", side_effect=RuntimeError("test-only-private-key")):
        failed = deployment_preflight(s.settings, s.database, vectors)
    assert not failed["ready"] and "test-only-private-key" not in str(failed)
    assert "test-only-private-key" not in str(report)


@pytest.mark.parametrize("fails", [False, True])
def test_backfill_queue_is_idempotent_and_failure_preserves_ready_document(durable, fails):
    s = durable
    s.settings.hierarchy_enabled = False
    document = upload(s)
    s.worker.run_once()
    original_leaves = list(s.vectors.chunks)
    s.settings.hierarchy_enabled = True
    enqueue(s.settings, s.database, s.uid, document.id)
    enqueue(s.settings, s.database, s.uid, document.id)
    assert s.queue.status(s.uid, document.id)["attempts"] == 0
    s.clock.now = datetime.now(timezone.utc) + timedelta(seconds=1)
    s.vectors.read_document_chunks = MagicMock(return_value=original_leaves,
        side_effect=UpstreamError("milvus", retryable=True) if fails else None)
    with patch.object(s.embeddings, "embed_documents", side_effect=AssertionError("must not embed old leaves")), \
         patch.object(s.vectors, "upsert", side_effect=AssertionError("must not rewrite old vectors")):
        assert s.worker.run_once()
    result = s.ingestion.get(s.uid, document.id)
    assert result.status == "ready" and result.chunk_count == len(original_leaves)
    assert s.vectors.chunks == original_leaves
    status = s.queue.status(s.uid, document.id)
    assert status["kind"] == "hierarchy" and status["state"] == ("queued" if fails else "succeeded")
    if not fails:
        assert result.parser_metadata["hierarchy"]["mode"] == "backfilled-leaves"


def test_backfill_rejects_another_owner(durable):
    s = durable
    document = upload(s)
    s.worker.run_once()
    s.settings.hierarchy_enabled = True
    from app.core.errors import AppError
    with pytest.raises(AppError):
        enqueue(s.settings, s.database, s.other, document.id)
    assert s.queue.status(s.uid, document.id)["kind"] == "ingest"
