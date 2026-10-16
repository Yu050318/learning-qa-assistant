from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import ServiceDependency, UserDependency
from app.application.model_center import ManagedModels, Provider
from app.core.errors import AppError

router = APIRouter(prefix="/api/v2/model-center", tags=["model center"])


class ConfigurationWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    configuration: ManagedModels


def center(services):
    if not services.settings.model_center_enabled:
        raise AppError("MODEL_CENTER_DISABLED", "模型中心尚未启用", 503)
    return services.model_center


@router.get("")
def configuration(services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return center(services).view(user_id)


@router.put("")
def update_configuration(payload: ConfigurationWrite, services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return center(services).update(user_id, payload.expected_revision, payload.configuration)


@router.post("/probe/{provider}")
def probe(provider: Provider, services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return center(services).probe(user_id, provider)


@router.get("/statistics")
def statistics(services: ServiceDependency, user_id: UserDependency, response: Response,
               days: Annotated[int, Query(ge=1, le=90)] = 30, global_scope: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return center(services).statistics(user_id, days, global_scope)
