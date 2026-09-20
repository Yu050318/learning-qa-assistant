import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from app.application.container import Services
from app.core.errors import AppError


def get_services(request: Request) -> Services:
    return request.app.state.services


def current_user(
    request: Request,
    services: Annotated[Services, Depends(get_services)],
    x_user_id: Annotated[str, Header(min_length=1, max_length=200)],
) -> UUID:
    identifier = x_user_id.strip()
    if not identifier or any(ord(character) < 32 for character in identifier):
        raise AppError("INVALID_USER_ID", "X-User-ID 不能为空或包含控制字符", 422)
    request.state.user_hash = hashlib.sha256(identifier.encode()).hexdigest()[:16]
    return services.user_id(identifier)


ServiceDependency = Annotated[Services, Depends(get_services)]
UserDependency = Annotated[UUID, Depends(current_user)]
