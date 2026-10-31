from app.open_api.evaluations_contracts import DatasetCreate
"""The evaluation API validates, freezes and queues; Workers perform all calls."""
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict

from app.web.dependencies import AdminDependency, ServiceDependency
from app.entity.evaluation.evaluation_dataset import WorkbenchDataset
from app.service.evaluation_jobs import EvaluationJobCreate

router = APIRouter(prefix="/api/v2/evaluations", tags=["evaluations"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]




def service(services, response):
    response.headers["Cache-Control"] = "no-store"
    return services.evaluations


def dataset_response(row, *, content=False):
    value = {name: getattr(row, name) for name in ("id", "name", "fingerprint", "case_count", "created_at")}
    if content:
        value["dataset"] = row.content
    return value


def job_response(row):
    value = {name: getattr(row, name) for name in ("id", "dataset_id", "dataset_fingerprint", "baseline_job_id", "split", "profile",
        "state", "gate_policy", "completed_cases", "total_cases", "generation", "attempts", "max_attempts", "error_code", "report", "started_at", "finished_at", "created_at")}
    snapshot = row.model_snapshot
    catalog = snapshot["catalog"]
    value["model_configuration_revision"] = snapshot["revision"]
    value["models"] = {role: {name: catalog["profiles"][key][name] for name in ("id", "version", "display_name", "provider", "model_name")}
        for role, key in {**catalog["assignments"], "answer": snapshot["answer_profile_id"]}.items()}
    value["document_ids"] = row.scope_snapshot["document_ids"]
    return value


def case_response(row):
    return {name: getattr(row, name) for name in ("id", "case_id", "position", "state", "label", "run_id", "prediction", "review", "result",
        "error_code", "duration_ms", "started_at", "finished_at")}


@router.get("/datasets")
def datasets(services: ServiceDependency, uid: AdminDependency, response: Response, offset: Offset = 0, limit: Limit = 20):
    return {"items": [dataset_response(row) for row in service(services, response).datasets(uid, offset, limit)]}


@router.post("/datasets", status_code=201)
def create_dataset(payload: DatasetCreate, services: ServiceDependency, uid: AdminDependency, response: Response):
    return dataset_response(service(services, response).create_dataset(uid, payload.dataset), content=True)


@router.get("/datasets/{dataset_id}")
def dataset(dataset_id: UUID, services: ServiceDependency, uid: AdminDependency, response: Response):
    return dataset_response(service(services, response).get_dataset(uid, dataset_id), content=True)


@router.post("/jobs", status_code=201)
def create_job(payload: EvaluationJobCreate, services: ServiceDependency, uid: AdminDependency, response: Response,
               client_key: Annotated[UUID, Header(alias="Idempotency-Key")]):
    return job_response(service(services, response).create_job(uid, payload, client_key))


@router.get("/jobs")
def jobs(services: ServiceDependency, uid: AdminDependency, response: Response, offset: Offset = 0, limit: Limit = 20,
         state: Literal["queued", "running", "cancelling", "cancelled", "succeeded", "failed"] | None = None):
    return {"items": [job_response(row) for row in service(services, response).jobs(uid, offset, limit, state)]}


@router.get("/jobs/{job_id}")
def job(job_id: UUID, services: ServiceDependency, uid: AdminDependency, response: Response):
    return job_response(service(services, response).get_job(uid, job_id))


@router.get("/jobs/{job_id}/cases")
def cases(job_id: UUID, services: ServiceDependency, uid: AdminDependency, response: Response, offset: Offset = 0, limit: Limit = 50):
    return {"items": [case_response(row) for row in service(services, response).cases(uid, job_id, offset, limit)]}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: UUID, services: ServiceDependency, uid: AdminDependency, response: Response):
    return job_response(service(services, response).cancel(uid, job_id))
