from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.dependencies import current_user
from app.application.artifacts import ArtifactService
from app.core.errors import AppError
from app.domain.artifacts import classify_artifact, render_artifact_preview
from app.domain.contracts import AgentChatInput
from app.domain.sandbox import SandboxFile
from app.infrastructure.persistence.models import (ChatRun, ChatRunEvent, ChatSession,
    Organization, OrganizationMember, RunArtifact, User)
from app.main import create_app
from test_sandbox_capabilities import sandbox, control, system, configure
from test_capability_control import fails


def begin(s, *, team=False):
    if team:
        s.settings.sharing_enabled = True
        s.settings.workspaces_enabled = True
        with s.database.sessions() as session:
            organization = Organization(name='Artifact team', owner_id=s.uid)
            session.add(organization); session.flush()
            session.add(OrganizationMember(organization_id=organization.id, user_id=s.uid, role='owner'))
            session.get(ChatSession, s.sid).organization_id = organization.id
            session.commit()
            s.organization_id = organization.id
    run = s.runs.start(s.uid, s.sid, AgentChatInput(question='calculate', skill_name='compute',
        approved_tools=('sandbox_execute',), approval_catalog_revision=3), 'artifact-test', uuid4())
    s.claim = s.store.claim_next(run.id)
    s.operation = 'tool/artifact-test'
    s.store.begin_operation(s.claim, s.operation, {'name': 'sandbox_execute'}, readonly=False)
    s.artifacts = ArtifactService(s.store, s.capabilities)
    return s


@pytest.fixture
def artifacts(sandbox):
    return begin(sandbox)


def save(s, *files):
    return s.artifacts.save_pending(s.claim, s.operation, files or (SandboxFile('answer.txt', b'42'),))


def publish(s):
    s.store.complete_operation(s.claim, s.operation, {'ok': True, 'value': {'success': True}})


def test_pending_is_hidden_and_publication_is_atomic_and_replay_is_unique(artifacts):
    s = artifacts
    first = save(s)
    second = save(s)
    assert first == second
    assert first[0]['tool_name'] == 'sandbox_execute' and first[0]['tool_version'] == 1
    assert 'content' not in first[0]
    assert s.artifacts.list(s.uid, s.claim.run_id) == []
    fails('NOT_FOUND', s.artifacts.read, s.uid, s.claim.run_id, first[0]['id'])
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunArtifact)) == 1
        assert not session.scalar(select(func.count()).select_from(ChatRunEvent).where(ChatRunEvent.event == 'artifacts_ready'))
    publish(s)
    assert s.artifacts.list(s.uid, s.claim.run_id) == first
    metadata, content, media = s.artifacts.read(s.uid, s.claim.run_id, first[0]['id'])
    assert content == b'42' and media == 'text/plain' and metadata['sha256'] == SandboxFile('x', b'42').sha256
    cached, _ = s.store.begin_operation(s.claim, s.operation, {'name': 'sandbox_execute'}, readonly=False)
    assert cached
    with s.database.sessions() as session:
        events = list(session.scalars(select(ChatRunEvent).where(ChatRunEvent.event == 'artifacts_ready')))
        assert len(events) == 1 and events[0].payload['items'][0]['id'] == str(first[0]['id'])


def test_different_replay_wrong_owner_and_stale_generation_cannot_write(artifacts):
    s = artifacts
    save(s)
    fails('RUN_CHECKPOINT_DIVERGED', save, s, SandboxFile('answer.txt', b'changed'))
    fails('RUN_LEASE_LOST', s.artifacts.save_pending, replace(s.claim, generation=s.claim.generation+1),
          s.operation, (SandboxFile('other.txt', b'x'),))
    fails('NOT_FOUND', s.artifacts.save_pending, replace(s.claim, user_id=s.other),
          s.operation, (SandboxFile('other.txt', b'x'),))


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a//b', 'a\\b', 'quote".txt', '.hidden', 'a/'*5+'b'])
def test_unsafe_names_are_rejected_before_storage(artifacts, name):
    fails('ARTIFACT_UNSAFE_NAME', save, artifacts, SandboxFile(name, b'x'))


def test_count_size_and_complete_batch_validation_leave_no_partial_files(artifacts):
    s = artifacts
    fails('ARTIFACT_INVALID_RESULT', save, s, SandboxFile('x', b'1'), SandboxFile('x', b'2'))
    fails('ARTIFACT_SIZE_LIMIT', save, s, SandboxFile('big', b'x'*(1048576+1)))
    fails('ARTIFACT_SIZE_LIMIT', save, s, *(SandboxFile(str(i), b'x') for i in range(9)))
    fails('ARTIFACT_INVALID_CONTENT', save, s, SandboxFile('good.txt', b'ok'), SandboxFile('bad.png', b'not-an-image'))
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunArtifact)) == 0


def test_run_wide_quota_counts_pending_files_too(artifacts, monkeypatch):
    s = artifacts
    monkeypatch.setattr('app.application.artifacts.MAX_RUN_FILES', 1)
    save(s)
    second = 'tool/artifact-second'
    s.store.begin_operation(s.claim, second, {'name': 'sandbox_execute', 'attempt': 2}, readonly=False)
    fails('ARTIFACT_RUN_LIMIT', s.artifacts.save_pending, s.claim, second, [SandboxFile('second.txt', b'x')])
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunArtifact)) == 1


def test_cancel_and_readonly_operations_cannot_create_pending_artifacts(artifacts):
    s = artifacts
    readonly = 'tool/read-only'
    s.store.begin_operation(s.claim, readonly, {'name': 'read'}, readonly=True)
    fails('RUN_CHECKPOINT_DIVERGED', s.artifacts.save_pending, s.claim, readonly, [SandboxFile('not-owned.txt', b'x')])
    s.store.request_cancel(s.uid, s.claim.run_id)
    fails('RUN_CANCEL_REQUESTED', save, s)
    with s.database.sessions() as session:
        assert session.scalar(select(func.count()).select_from(RunArtifact)) == 0


def test_cross_user_run_and_changed_content_are_rejected(artifacts):
    s = artifacts
    artifact = save(s)[0]
    publish(s)
    fails('NOT_FOUND', s.artifacts.read, s.other, s.claim.run_id, artifact['id'])
    fails('NOT_FOUND', s.artifacts.read, s.uid, uuid4(), artifact['id'])
    with s.database.sessions() as session:
        session.get(RunArtifact, artifact['id']).sha256 = 'a'*64
        session.commit()
    fails('ARTIFACT_CONTENT_CHANGED', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])


@pytest.mark.parametrize('state', ['cancelling', 'cancelled', 'failed', 'interrupted'])
def test_unsuccessful_run_hides_committed_tool_artifacts_but_preserves_audit(artifacts, state):
    s = artifacts
    artifact = save(s)[0]
    publish(s)
    with s.database.sessions() as session:
        session.get(ChatRun, s.claim.run_id).state = state
        session.commit()
    assert s.artifacts.list(s.uid, s.claim.run_id) == []
    fails('NOT_FOUND', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])
    with s.database.sessions() as session:
        assert session.get(RunArtifact, artifact['id']) is not None


def test_role_feature_and_tool_revocation_apply_to_downloads(artifacts):
    s = artifacts
    artifact = save(s)[0]
    publish(s)
    s.settings.sandbox_enabled = False
    fails('CAPABILITY_SCOPE_CHANGED', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])
    s.settings.sandbox_enabled = True
    configure(s, enabled=False)
    fails('CAPABILITY_SCOPE_CHANGED', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])
    configure(s)
    with s.database.sessions() as session:
        session.get(User, s.uid).role = 'user'
        session.commit()
    fails('CAPABILITY_SCOPE_CHANGED', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])


def test_workspace_membership_revocation_blocks_download_and_session_delete_cascades(sandbox):
    s = begin(sandbox, team=True)
    artifact = save(s)[0]
    publish(s)
    assert s.artifacts.list(s.uid, s.claim.run_id)[0]['organization_id'] == s.organization_id
    with s.database.sessions() as session:
        session.delete(session.get(OrganizationMember, (s.organization_id, s.uid)))
        session.commit()
    fails('NOT_FOUND', s.artifacts.read, s.uid, s.claim.run_id, artifact['id'])
    with s.database.sessions() as session:
        session.delete(session.get(ChatSession, s.sid))
        session.commit()
        assert session.get(RunArtifact, artifact['id']) is None


def test_download_and_preview_are_authenticated_non_executable_and_not_cached(artifacts):
    s = artifacts
    records = save(s, SandboxFile('script.html', b'<script>alert(1)</script>'),
                   SandboxFile('literal.txt', b'<svg onload=alert(1)>'))
    publish(s)
    s.close = lambda: None
    app = create_app(s.settings, s)
    app.dependency_overrides[current_user] = lambda: s.uid
    with TestClient(app) as client:
        path = f'/api/v2/runs/{s.claim.run_id}/artifacts'
        listed = client.get(path)
        assert listed.status_code == 200 and listed.headers['cache-control'] == 'no-store'
        record = next(item for item in records if item['name'] == 'script.html')
        download = client.get(f"{path}/{record['id']}/download")
        assert download.content == b'<script>alert(1)</script>'
        assert download.headers['content-type'] == 'application/octet-stream'
        assert download.headers['content-disposition'].startswith('attachment;')
        assert download.headers['x-content-type-options'] == 'nosniff'
        assert "default-src 'none'" in download.headers['content-security-policy']
        assert client.get(f"{path}/{record['id']}/preview").status_code == 415
        text = next(item for item in records if item['name'] == 'literal.txt')
        preview = client.get(f"{path}/{text['id']}/preview")
        assert preview.content == b'<svg onload=alert(1)>'
        assert preview.headers['content-type'].startswith('text/plain')
        app.dependency_overrides[current_user] = lambda: s.other
        assert client.get(f"{path}/{record['id']}/download").status_code == 404


@pytest.mark.parametrize('extension,format_name', [('png','PNG'), ('jpg','JPEG'), ('webp','WEBP')])
def test_image_format_is_decoded_and_preview_is_fresh_png(extension, format_name):
    buffer = BytesIO()
    Image.new('RGB', (3, 2), 'green').save(buffer, format=format_name)
    content = buffer.getvalue()
    media, kind = classify_artifact('image.'+extension, content)
    assert kind == 'image'
    file = SandboxFile('image.'+extension, content)
    row = SimpleNamespace(name=file.name, content=content, sha256=file.sha256,
                          size_bytes=len(content), media_type=media, preview_kind=kind)
    preview, preview_media = render_artifact_preview(row)
    assert preview_media == 'image/png'
    with Image.open(BytesIO(preview)) as image:
        assert image.format == 'PNG' and image.size == (3, 2) and not image.info
    fails('ARTIFACT_INVALID_CONTENT', classify_artifact, 'wrong.jpg' if extension != 'jpg' else 'wrong.png', content)


def test_text_preview_is_bounded_and_binary_or_active_content_is_not_previewable():
    content = b'x'*20000
    file = SandboxFile('large.txt', content)
    row = SimpleNamespace(name=file.name, content=content, sha256=file.sha256,
                          size_bytes=len(content), media_type='text/plain', preview_kind='text')
    assert len(render_artifact_preview(row)[0]) == 16384
    for name, content in [('binary.txt', b'\x00bad'), ('active.svg', b'<svg/>')]:
        assert classify_artifact(name, content) == ('application/octet-stream', 'none')
