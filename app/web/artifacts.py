"""Authenticated artifact reads, never static file URLs or executable previews."""
from uuid import UUID

from fastapi import APIRouter, Response
from app.web.dependencies import ServiceDependency, UserDependency
from app.core.errors import AppError
from app.entity.bo.artifacts import PREVIEW_TEXT_BYTES

router = APIRouter(prefix='/api/v2/runs/{run_id}/artifacts', tags=['run artifacts'])


def service(services):
    if services.runs is None or getattr(services, 'artifacts', None) is None:
        raise AppError('ARTIFACTS_UNAVAILABLE', '运行产物服务尚未启用', 503)
    return services.artifacts


@router.get('')
def list_artifacts(run_id: UUID, services: ServiceDependency, user_id: UserDependency, http_response: Response):
    http_response.headers.update({'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
    return {'items': service(services).list(user_id, run_id)}


def response(services, user_id, run_id, artifact_id, preview):
    metadata, content, media_type = service(services).read(user_id, run_id, artifact_id, preview=preview)
    disposition = 'inline' if preview else 'attachment'
    # Names are ASCII path segments. Basename never becomes a filesystem path.
    filename = metadata['name'].rsplit('/', 1)[-1]
    return Response(content, media_type=media_type, headers={
        'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'none'; sandbox; frame-ancestors 'none'",
        'Cross-Origin-Resource-Policy': 'same-origin',
        'Content-Disposition': f'{disposition}; filename="{filename}"',
        'X-Artifact-SHA256': metadata['sha256'],
        'X-Artifact-Size': str(metadata['size_bytes']),
        'X-Preview-Truncated': str(preview and metadata['preview_kind'] == 'text' and metadata['size_bytes'] > PREVIEW_TEXT_BYTES).lower(),
    })


@router.get('/{artifact_id}/download')
def download_artifact(run_id: UUID, artifact_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return response(services, user_id, run_id, artifact_id, False)


@router.get('/{artifact_id}/preview')
def preview_artifact(run_id: UUID, artifact_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return response(services, user_id, run_id, artifact_id, True)
