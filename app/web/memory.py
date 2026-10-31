"""HTTP transport for opt-in memory; identity is always authenticated server state."""
from uuid import UUID

from fastapi import APIRouter, Response

from app.open_api.memory import AutomaticMemoryResponse, MemorySettingsResponse, MemorySettingsWrite
from app.web.dependencies import ServiceDependency, UserDependency

router = APIRouter(prefix='/api/v2', tags=['automatic memory'])


@router.get('/memory-settings', response_model=MemorySettingsResponse)
def memory_settings(services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return services.automatic_memories.get_settings(user_id)


@router.patch('/memory-settings', response_model=MemorySettingsResponse)
def update_memory_settings(payload: MemorySettingsWrite, services: ServiceDependency, user_id: UserDependency):
    return services.automatic_memories.set_enabled(user_id, payload.automatic_enabled)


@router.get('/automatic-memories', response_model=list[AutomaticMemoryResponse])
def automatic_memories(services: ServiceDependency, user_id: UserDependency, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return [AutomaticMemoryResponse.model_validate(row) for row in services.automatic_memories.list(user_id)]


@router.delete('/automatic-memories', status_code=202, response_model=MemorySettingsResponse)
def clear_automatic_memories(services: ServiceDependency, user_id: UserDependency):
    return services.automatic_memories.clear(user_id)


@router.delete('/automatic-memories/{memory_id}', status_code=202)
def delete_automatic_memory(memory_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.automatic_memories.delete(user_id, memory_id)
    return {'status': 'deleting'}
