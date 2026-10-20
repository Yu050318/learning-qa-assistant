from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.application.evaluation_dataset import WorkbenchDataset
from app.application.evaluation_jobs import EvaluationJobCreate, EvaluationService
from app.application.runs import RunStore
from app.application.sharing import SharingService
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import ChatSession, DocumentRecord, EvaluationCaseResult, EvaluationJob, User
from app.main import create_app
from app.api.schemas import ContextEstimateRequest
from test_api_v2 import Services
from test_model_center import center
from test_chat_runs import system


@pytest.fixture
def evaluation(center):
    s = center
    s.settings.evaluation_enabled = True
    s.settings.knowledge_quality_enabled = True
    s.evaluation = EvaluationService(s.settings, s.database)
    with s.database.sessions() as session:
        doc = DocumentRecord(user_id=s.uid, original_name="specs.txt", file_type=".txt", file_path="fixture",
            content_hash="d" * 64, status="ready", chunk_count=1, embedding_model=s.settings.embedding_model)
        session.add(doc)
        session.commit()
        s.document_id = doc.id
    labels = WorkbenchDataset.model_validate({"name": "fixture", "cases": [
        {"id": "fact", "category": "facts", "question": "功率？", "expected_kinds": ["grounded"], "expected_source_types": ["knowledge"],
         "expected_facts": ["功率100W"], "split": "dev", "gold_anchors": [{"canonical_name": "specs.txt", "section": "power"}]},
        {"id": "missing", "category": "missing", "question": "NX-999？", "expected_kinds": ["insufficient"], "split": "frozen"}]})
    s.dataset = s.evaluation.create_dataset(s.uid, labels)
    s.request = EvaluationJobCreate(dataset_id=s.dataset.id, document_ids=[s.document_id])
    return s


def denied(function, *args, code):
    with pytest.raises(AppError) as failure:
        function(*args)
    assert failure.value.code == code


def test_dataset_deduplicates_and_admin_cannot_read_another_owners_labels(evaluation):
    s = evaluation
    again = s.evaluation.create_dataset(s.uid, WorkbenchDataset.model_validate(s.dataset.content))
    assert again.id == s.dataset.id and len(s.evaluation.datasets(s.uid)) == 1
    denied(s.evaluation.create_dataset, s.other, WorkbenchDataset.model_validate(s.dataset.content), code="FORBIDDEN")
    with s.database.sessions() as session:
        session.get(User, s.other).role = "admin"
        session.commit()
    assert s.evaluation.datasets(s.other) == []
    denied(s.evaluation.get_dataset, s.other, s.dataset.id, code="NOT_FOUND")


def test_job_freezes_models_scope_labels_and_profile_and_retries_without_refreezing(evaluation):
    s = evaluation
    key = uuid4()
    job = s.evaluation.create_job(s.uid, s.request.model_copy(update={"profile": "baseline"}), key)
    assert job.state == "queued" and job.total_cases == 2
    assert not job.model_snapshot["settings"]["knowledge_quality_enabled"]
    assert job.scope_snapshot["document_ids"] == [str(s.document_id)]
    assert "fixture-not-real-key" not in str(job.model_snapshot)
    previous = job.model_snapshot["catalog"]["profiles"]["deepseek"]["model_name"]
    configuration = s.center.snapshot().configuration.model_copy(deep=True)
    configuration.profiles["deepseek"].model_name = "updated-model"
    s.center.update(s.uid, 1, configuration)
    same = s.evaluation.create_job(s.uid, s.request.model_copy(update={"profile": "baseline"}), key)
    assert same.id == job.id and same.model_snapshot["catalog"]["profiles"]["deepseek"]["model_name"] == previous
    denied(s.evaluation.create_job, s.uid, s.request, key, code="IDEMPOTENCY_CONFLICT")
    cases = s.evaluation.cases(s.uid, job.id)
    assert [case.case_id for case in cases] == ["fact", "missing"] and all(case.run_id is None for case in cases)


def test_enabling_bound_indexes_never_refreezes_an_old_evaluation_scope(evaluation):
    s = evaluation
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    original_scope, original_models = dict(job.scope_snapshot), dict(job.model_snapshot)
    assert "indexes" not in original_scope
    s.settings.document_publication_enabled = True
    with s.database.sessions() as session:
        with pytest.raises(AppError) as failure:
            s.evaluation.validate_scope(session, session.get(EvaluationJob, job.id))
    assert failure.value.code == "DOCUMENT_SCOPE_CHANGED"
    with s.database.sessions() as session:
        row = session.get(EvaluationJob, job.id)
        assert row.scope_snapshot == original_scope and row.model_snapshot == original_models


def test_foreign_documents_invalid_gold_and_empty_splits_never_queue_a_job(evaluation):
    s = evaluation
    denied(s.evaluation.create_job, s.uid, s.request.model_copy(update={"document_ids": [uuid4()]}), uuid4(), code="NOT_FOUND")
    denied(s.evaluation.create_job, s.uid, s.request.model_copy(update={"split": "custom"}), uuid4(), code="EVALUATION_EMPTY_SPLIT")
    with s.database.sessions() as session:
        session.get(DocumentRecord, s.document_id).original_name = "renamed.txt"
        session.commit()
    denied(s.evaluation.create_job, s.uid, s.request, uuid4(), code="EVALUATION_GOLD_SCOPE_INVALID")
    assert s.evaluation.jobs(s.uid) == []


def test_takeover_fences_old_owner_and_cancelled_jobs_never_execute(evaluation):
    s = evaluation
    job = s.evaluation.create_job(s.uid, s.request, uuid4())
    first = s.evaluation.claim_next()
    assert first.job_id == job.id and s.evaluation.claim_next() is None
    s.evaluation.heartbeat(first)
    with s.database.sessions() as session:
        session.get(EvaluationJob, job.id).lease_expires_at = s.evaluation.now(session) - timedelta(seconds=1)
        session.commit()
    second = s.evaluation.claim_next()
    assert second.generation == first.generation + 1 and second.token != first.token
    denied(s.evaluation.heartbeat, first, code="EVALUATION_LEASE_LOST")
    s.evaluation.cancel(s.uid, job.id)
    denied(s.evaluation.check_owned, second, code="EVALUATION_CANCEL_REQUESTED")
    with s.database.sessions() as session:
        session.get(EvaluationJob, job.id).lease_expires_at = s.evaluation.now(session) - timedelta(seconds=1)
        session.commit()
    assert s.evaluation.claim_next() is None
    assert s.evaluation.get_job(s.uid, job.id).state == "cancelled"
    assert all(case.state == "cancelled" for case in s.evaluation.cases(s.uid, job.id))
    queued = s.evaluation.create_job(s.uid, s.request, uuid4())
    assert s.evaluation.cancel(s.uid, queued.id).state == "cancelled"
    assert s.evaluation.claim_next() is None


def test_queue_capacity_exact_retry_and_cross_admin_job_privacy(evaluation):
    s = evaluation
    s.settings.evaluation_queue_limit = 1
    key = uuid4()
    job = s.evaluation.create_job(s.uid, s.request, key)
    assert s.evaluation.create_job(s.uid, s.request, key).id == job.id
    denied(s.evaluation.create_job, s.uid, s.request, uuid4(), code="EVALUATION_CAPACITY_EXCEEDED")
    with s.database.sessions() as session:
        session.get(User, s.other).role = "admin"
        session.commit()
    denied(s.evaluation.get_job, s.other, job.id, code="NOT_FOUND")
    denied(s.evaluation.cancel, s.other, job.id, code="NOT_FOUND")


def test_evaluation_run_queue_and_sessions_are_isolated_from_chat(evaluation):
    s = evaluation
    with s.database.sessions() as session:
        hidden = ChatSession(user_id=s.uid, title="evaluation", kind="evaluation")
        session.add(hidden)
        session.commit()
    store = RunStore(s.settings, s.database, kind="evaluation")
    run, created = store.create(s.uid, hidden.id, AgentChatInput("fixture", "knowledge"), "evaluation-run", uuid4(), queued=True, prepare=s.chat.prepare_run)
    assert created and s.store.claim_next() is None
    claim = store.claim_next(run.id)
    assert claim.run_id == run.id
    denied(s.store.read, s.uid, run.id, code="NOT_FOUND")
    assert all(row.id != hidden.id for row in s.sessions.list(s.uid, 0, 20))
    denied(s.sessions.get, s.uid, hidden.id, code="NOT_FOUND")
    denied(s.sessions.delete, s.uid, hidden.id, code="NOT_FOUND")
    denied(s.chat.estimate_context, s.uid, ContextEstimateRequest(question="fixture", session_id=hidden.id), code="NOT_FOUND")
    denied(s.chat.compact, s.uid, hidden.id, "fixture", None, code="NOT_FOUND")
    denied(s.store.create, s.uid, hidden.id, AgentChatInput("fixture", "knowledge"), "chat", uuid4(), code="NOT_FOUND")


def test_api_returns_safe_snapshots_queues_only_and_blocks_ordinary_users(evaluation):
    s = evaluation
    services = Services()
    services.settings, services.database, services.evaluations, services.current_user = s.settings, s.database, s.evaluation, s.uid
    headers = {"Authorization": "Bearer fixture", "Idempotency-Key": str(uuid4())}
    with TestClient(create_app(s.settings, services)) as client:
        response = client.post("/api/v2/evaluations/jobs", headers=headers, json=s.request.model_dump(mode="json"))
        assert response.status_code == 201
        body = response.json()
        assert body["state"] == "queued" and body["models"]["evaluator"]["id"] == "deepseek"
        assert "fixture-not-real-key" not in response.text and "base_url" not in response.text
        assert response.headers["cache-control"] == "no-store"
        job_id = body["id"]
        assert len(client.get(f"/api/v2/evaluations/jobs/{job_id}/cases", headers=headers).json()["items"]) == 2
        assert client.post("/api/v2/evaluations/jobs", headers={"Authorization": "Bearer fixture"}, json=s.request.model_dump(mode="json")).status_code == 422
        services.current_user = s.other
        assert client.get("/api/v2/evaluations/datasets", headers=headers).status_code == 403
        assert client.post(f"/api/v2/evaluations/jobs/{job_id}/cancel", headers=headers).status_code == 403
    assert s.workflow.calls == 0


def test_endpoint_credentials_are_rejected_before_persisting_job_snapshots(evaluation):
    s = evaluation
    s.settings.deepseek_base_url = "https://user:fixture-private-password@example.com"
    denied(s.evaluation.create_job, s.uid, s.request, uuid4(), code="INVALID_MODEL_ENDPOINT")
    assert s.evaluation.jobs(s.uid) == []


def test_shared_library_scope_excludes_unselected_private_documents_and_rechecks_revocation(evaluation):
    s = evaluation
    s.settings.sharing_enabled = True
    sharing = SharingService(s.settings, s.database)
    organization = sharing.create_organization(s.other, "shared fixture")
    sharing.set_member(s.other, organization["id"], "run-owner", "member")
    library = sharing.create_base(s.other, organization["id"], "shared specs")
    with s.database.sessions() as session:
        doc = DocumentRecord(user_id=s.other, original_name="specs.txt", file_type=".txt", file_path="fixture",
            content_hash="b" * 64, status="ready", embedding_model=s.settings.embedding_model, chunk_count=1)
        session.add(doc)
        session.commit()
    sharing.bind(s.other, library["id"], doc.id)
    sharing.set_grant(s.other, library["id"], s.uid, "viewer")
    payload = EvaluationJobCreate(dataset_id=s.dataset.id, knowledge_base_ids=[library["id"]])
    job = s.evaluation.create_job(s.uid, payload, uuid4())
    assert job.scope_snapshot["document_ids"] == [str(doc.id)]
    assert str(s.document_id) not in job.scope_snapshot["document_ids"]
    claim = s.evaluation.claim_next()
    s.evaluation.check_owned(claim)
    sharing.set_grant(s.other, library["id"], s.uid, None)
    denied(s.evaluation.check_owned, claim, code="DOCUMENT_SCOPE_CHANGED")


def test_evaluation_freezes_library_current_not_owners_private_pointer_or_shared_history(evaluation):
    from app.infrastructure.persistence.repository import Repository
    s = evaluation
    s.settings.sharing_enabled = True
    sharing = SharingService(s.settings, s.database)
    organization = sharing.create_organization(s.uid, "version fixture")
    library = sharing.create_base(s.uid, organization["id"], "version specs")["id"]
    with s.database.sessions() as session:
        repo = Repository(session)
        first = repo.document(s.uid, s.document_id)
        repo.bind_document_version(s.uid, first)
        newer = DocumentRecord(user_id=s.uid, original_name="specs.txt", file_type=".txt", file_path="new-fixture",
            content_hash="e" * 64, status="ready", embedding_model=s.settings.embedding_model, chunk_count=1)
        session.add(newer)
        session.flush()
        repo.bind_document_version(s.uid, newer, first.id)
        session.commit()
    sharing.bind(s.uid, library, first.id)
    sharing.bind(s.uid, library, newer.id, expected_current=first.id)
    assert sharing.scope(s.uid, [library], include_owned=False) == {newer.id:s.uid}
    job = s.evaluation.create_job(s.uid, EvaluationJobCreate(dataset_id=s.dataset.id, knowledge_base_ids=[library]), uuid4())
    assert job.scope_snapshot["document_ids"] == [str(newer.id)]
    # Changing the library pointer does not revoke historical published access.
    sharing.bind(s.uid, library, first.id, expected_current=newer.id)
    with s.database.sessions() as session:
        s.evaluation.validate_scope(session, job)
    sharing.bind(s.uid, library, newer.id, remove=True)
    denied(s.evaluation.create_job, s.uid, EvaluationJobCreate(dataset_id=s.dataset.id,
        knowledge_base_ids=[library], document_ids=[newer.id]), uuid4(), code="NOT_FOUND")
    with s.database.sessions() as session, pytest.raises(AppError) as failure:
        s.evaluation.validate_scope(session, job)
    assert failure.value.code == "DOCUMENT_SCOPE_CHANGED"
