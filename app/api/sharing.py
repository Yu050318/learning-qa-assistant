from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import ServiceDependency, UserDependency
from app.core.errors import AppError

router = APIRouter(tags=["sharing"])


class Named(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class MemberWrite(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    username: str = Field(pattern=r"^[a-z0-9_-]{3,32}$")
    role: Literal["admin", "member"] = "member"


class GrantWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["viewer", "editor"]


class PublishVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_current_document_id: UUID | None


def sharing(services):
    if not services.settings.sharing_enabled:
        raise AppError("SHARING_DISABLED", "组织共享尚未启用", 503)
    return services.sharing


@router.get("/workspaces")
def workspaces(services: ServiceDependency, user_id: UserDependency):
    from app.application.workspaces import list_workspaces
    return list_workspaces(services.settings, services.database, user_id)


@router.get("/organizations")
def organizations(services: ServiceDependency, user_id: UserDependency):
    return sharing(services).organizations(user_id)


@router.post("/organizations", status_code=201)
def create_org(payload: Named, services: ServiceDependency, user_id: UserDependency):
    return sharing(services).create_organization(user_id, payload.name)


@router.delete("/organizations/{organization_id}", status_code=204)
def delete_org(organization_id: UUID, services: ServiceDependency, user_id: UserDependency):
    sharing(services).delete_organization(user_id, organization_id)
    return Response(status_code=204)


@router.get("/organizations/{organization_id}/members")
def members(organization_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return sharing(services).members(user_id, organization_id)


@router.put("/organizations/{organization_id}/members", status_code=204)
def set_member(organization_id: UUID, payload: MemberWrite, services: ServiceDependency, user_id: UserDependency):
    sharing(services).set_member(user_id, organization_id, payload.username, payload.role)
    return Response(status_code=204)


@router.delete("/organizations/{organization_id}/members/{target_id}", status_code=204)
def remove_member(organization_id: UUID, target_id: UUID, services: ServiceDependency, user_id: UserDependency):
    sharing(services).remove_member(user_id, organization_id, target_id)
    return Response(status_code=204)


@router.get("/knowledge-bases")
def knowledge_bases(services: ServiceDependency, user_id: UserDependency):
    return sharing(services).bases(user_id)


@router.post("/organizations/{organization_id}/knowledge-bases", status_code=201)
def create_base(organization_id: UUID, payload: Named, services: ServiceDependency, user_id: UserDependency):
    return sharing(services).create_base(user_id, organization_id, payload.name)


@router.delete("/knowledge-bases/{base_id}", status_code=204)
def delete_base(base_id: UUID, services: ServiceDependency, user_id: UserDependency):
    sharing(services).delete_base(user_id, base_id)
    return Response(status_code=204)


@router.get("/knowledge-bases/{base_id}/grants")
def grants(base_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return sharing(services).grants(user_id, base_id)


@router.put("/knowledge-bases/{base_id}/grants/{target_id}", status_code=204)
def set_grant(base_id: UUID, target_id: UUID, payload: GrantWrite, services: ServiceDependency, user_id: UserDependency):
    sharing(services).set_grant(user_id, base_id, target_id, payload.role)
    return Response(status_code=204)


@router.delete("/knowledge-bases/{base_id}/grants/{target_id}", status_code=204)
def revoke_grant(base_id: UUID, target_id: UUID, services: ServiceDependency, user_id: UserDependency):
    sharing(services).set_grant(user_id, base_id, target_id, None)
    return Response(status_code=204)


@router.get("/knowledge-bases/{base_id}/documents")
def documents(base_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return sharing(services).documents(user_id, base_id)


@router.put("/knowledge-bases/{base_id}/documents/{document_id}", status_code=204)
def publish(base_id: UUID, document_id: UUID, payload: PublishVersion, services: ServiceDependency, user_id: UserDependency):
    sharing(services).bind(user_id, base_id, document_id, expected_current=payload.expected_current_document_id)
    return Response(status_code=204)


@router.delete("/knowledge-bases/{base_id}/documents/{document_id}", status_code=204)
def unpublish(base_id: UUID, document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    sharing(services).bind(user_id, base_id, document_id, remove=True)
    return Response(status_code=204)
