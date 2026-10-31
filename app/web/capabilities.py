from app.open_api.capabilities_contracts import RevisionRequest, SkillWrite, ToolWrite, SqlWrite, WebWrite, SandboxWrite
"""Administrator writes and role-filtered discovery use the same stored catalog."""
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.web.dependencies import AdminDependency, ServiceDependency, UserDependency
from app.entity.bo.capabilities import HttpToolDeclaration, SkillDeclaration, SqlPolicy, WebPolicy, SandboxPolicy

router = APIRouter(prefix="/api/v2/capability-center", tags=["capability center"])














@router.put("/runtime/sandbox")
def update_sandbox(payload: SandboxWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.update_runtime(user_id, "sandbox", payload.declaration, payload.expected_revision)


@router.put("/runtime/sql")
def update_sql(payload: SqlWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.update_runtime(user_id, "sql", payload.declaration, payload.expected_revision)


@router.put("/runtime/web")
def update_web(payload: WebWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.update_runtime(user_id, "web", payload.declaration, payload.expected_revision)


@router.get("")
def catalog(services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return services.capabilities.public_catalog(user_id)


@router.get("/control-plane")
def control_plane(services: ServiceDependency, user_id: AdminDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return services.capabilities.control_plane(user_id)


@router.get("/skills/{name}")
def skill_detail(name: str, services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return services.capabilities.describe_skill(user_id, name)


@router.get("/versions")
def versions(services: ServiceDependency, user_id: AdminDependency, response: Response,
             offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=50)] = 20):
    response.headers["Cache-Control"] = "no-store"
    return services.capabilities.history(user_id, offset, limit)


@router.post("/skills", status_code=201)
def create_skill(payload: SkillWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.change(user_id, "skills", payload.declaration.name,
        expected_revision=payload.expected_revision, declaration=payload.declaration, create=True)


@router.put("/skills/{name}")
def update_skill(name: str, payload: SkillWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.change(user_id, "skills", name,
        expected_revision=payload.expected_revision, declaration=payload.declaration)


@router.post("/tools", status_code=201)
def create_tool(payload: ToolWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.change(user_id, "tools", payload.declaration.name,
        expected_revision=payload.expected_revision, declaration=payload.declaration, create=True)


@router.put("/tools/{name}")
def update_tool(name: str, payload: ToolWrite, services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.change(user_id, "tools", name,
        expected_revision=payload.expected_revision, declaration=payload.declaration)


@router.delete("/{kind}/{name}")
def delete_capability(kind: Literal["skills", "tools"], name: str, payload: RevisionRequest,
                      services: ServiceDependency, user_id: AdminDependency):
    return services.capabilities.change(user_id, kind, name, expected_revision=payload.expected_revision, delete=True)
