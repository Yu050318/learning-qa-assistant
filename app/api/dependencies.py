from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application.auth import AuthContext
from app.application.container import Services
from app.core.errors import AppError


def get_services(request: Request) -> Services:
    return request.app.state.services


bearer = HTTPBearer(auto_error=False)


def current_auth(
    request: Request,
    services: Annotated[Services, Depends(get_services)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError("UNAUTHORIZED", "请先登录", 401)
    context = services.auth.authenticate(credentials.credentials)
    request.state.user_hash = str(context.user_id).replace("-", "")[:16]
    return context


ServiceDependency = Annotated[Services, Depends(get_services)]
AuthDependency = Annotated[AuthContext, Depends(current_auth)]


def current_user(context: AuthDependency) -> UUID:
    return context.user_id


UserDependency = Annotated[UUID, Depends(current_user)]


def platform_admin(services: ServiceDependency, user_id: UserDependency) -> UUID:
    from app.application.permissions import require_platform_admin
    require_platform_admin(services.database, user_id)
    return user_id


AdminDependency = Annotated[UUID, Depends(platform_admin)]
