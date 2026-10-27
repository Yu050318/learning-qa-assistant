import hashlib
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import current_user
from app.application.preflight import capability_preflight
from app.application.workers import WorkerRegistry
from app.domain.capabilities import (CapabilityCatalog, CapabilityExecution,
    SandboxPolicy, builtin_tools, catalog_digest)
from app.domain.contracts import AgentChatInput
from app.domain.sandbox import SandboxLimits
from app.domain.tool_approvals import RunToolApproval
from app.infrastructure.persistence.models import User
from app.main import create_app
from test_capability_control import control, fails, http_tool, save, skill, system


def policy(**updates):
    return SandboxPolicy(**{'enabled': True, 'image': 'sha256:'+'b'*64, **updates})


def configure(s, **updates):
    return s.capabilities.update_runtime(s.uid, 'sandbox', policy(**updates), s.capabilities.snapshot()[0])


@pytest.fixture
def sandbox(control):
    control.settings.sandbox_enabled = True
    control.settings.tool_approvals_enabled = True
    configure(control)
    save(control, skill(name='compute', allowed_tools=('sandbox_execute',)))
    return control


def test_old_catalog_serialization_and_digest_are_byte_for_byte_compatible():
    legacy = {'schema_version': 1, 'skills': {}, 'tools': {}, 'retired_skills': [],
              'retired_tools': [], 'sql': None, 'web': None}
    old = CapabilityCatalog.model_validate_json(json.dumps(legacy))
    assert old.model_dump(mode='json') == legacy
    assert json.loads(old.model_dump_json()) == legacy
    expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert catalog_digest(old) == expected
    assert 'sandbox_execute' not in builtin_tools(old)
    assert catalog_digest(CapabilityCatalog(sandbox=None)) == expected


def test_existing_http_identity_and_retirement_cannot_become_sandbox(control):
    s = control
    save(s, http_tool(name='sandbox_execute'))
    frozen = s.capabilities.freeze(s.uid, search_mode='web', public_query='public data')
    assert frozen.allowed_tools == ('delegate_web', 'sandbox_execute')
    assert not frozen.approved_tools and frozen.catalog.sandbox is None
    assert s.capabilities.tool_reason(frozen.catalog, 'user', 'sandbox_execute') is None
    fails('SANDBOX_IDENTITY_CONFLICT', configure, s)
    assert s.capabilities.snapshot()[0] == 2
    s.capabilities.change(s.uid, 'tools', 'sandbox_execute', expected_revision=2, delete=True)
    fails('SANDBOX_IDENTITY_CONFLICT', configure, s)
    assert s.capabilities.snapshot()[0] == 3
    assert s.capabilities.history(s.uid)['items'][-1]['revision'] == 1


def test_registered_sandbox_identity_is_protected_and_configuration_uses_cas(sandbox):
    s = sandbox
    fails('BUILTIN_TOOL_PROTECTED', save, s, http_tool(name='sandbox_execute'))
    fails('BUILTIN_TOOL_PROTECTED', s.capabilities.change, s.uid, 'tools',
          'sandbox_execute', expected_revision=3, delete=True)
    fails('CAPABILITY_REVISION_CONFLICT', s.capabilities.update_runtime,
          s.uid, 'sandbox', policy(), 1)
    fails('FORBIDDEN', s.capabilities.update_runtime, s.other, 'sandbox', policy(), 3)
    assert s.capabilities.snapshot()[0] == 3


def test_sandbox_requires_run_bound_approval_without_public_input(sandbox):
    s = sandbox
    assert 'sandbox_execute' not in s.capabilities.freeze(s.uid).allowed_tools
    fails('TOOL_APPROVAL_REQUIRED', s.capabilities.freeze, s.uid, skill_name='compute')
    fails('FORBIDDEN', s.capabilities.freeze, s.other, approved_tools=('sandbox_execute',),
          approval_catalog_revision=3)
    request = AgentChatInput(question='calculate 6 * 7', skill_name='compute',
        approved_tools=('sandbox_execute',), approval_catalog_revision=3)
    run = s.runs.start(s.uid, s.sid, request, 'sandbox-catalog-test', uuid4())
    context = run.execution_snapshot['context']
    frozen = CapabilityExecution.model_validate_json(json.dumps(context['capability_snapshot']))
    grant = RunToolApproval.model_validate_json(json.dumps(context['tool_approval']))
    assert frozen.allowed_tools == frozen.approved_tools == ('sandbox_execute',)
    assert frozen.catalog.sandbox.version == 1
    assert grant.allows('sandbox_execute', user_id=s.uid, session_id=s.sid,
                        run_id=run.id, organization_id=None, catalog_hash=frozen.catalog_hash)
    # The existing auto mode may prepare a public-query string for other tools;
    # Sandbox freezing itself has no public-input prerequisite.
    assert s.capabilities.freeze(s.uid, skill_name='compute', approved_tools=('sandbox_execute',),
                                approval_catalog_revision=3).allowed_tools == ('sandbox_execute',)
    raw = frozen.model_dump(mode='json')
    raw['approved_tools'] = []
    with pytest.raises(ValidationError):
        CapabilityExecution.model_validate_json(json.dumps(raw))
    fails('SKILL_MODE_CONFLICT', s.capabilities.freeze, s.uid, skill_name='compute',
        search_mode='web', public_query='public', approved_tools=('sandbox_execute',), approval_catalog_revision=3)


def test_frozen_policy_is_retained_but_disabling_or_revoking_stops_execution(sandbox):
    s = sandbox
    frozen = s.capabilities.freeze(s.uid, skill_name='compute', approved_tools=('sandbox_execute',),
                                  approval_catalog_revision=3)
    configure(s, image='sha256:'+'c'*64, limits=SandboxLimits(timeout_seconds=2.0))
    checked = s.capabilities.validate_execution(s.uid, frozen, tool_name='sandbox_execute')
    assert checked.catalog.sandbox.version == 1
    assert checked.catalog.sandbox.declaration.image == 'sha256:'+'b'*64
    assert checked.catalog.sandbox.declaration.limits.timeout_seconds == 15
    assert s.capabilities.snapshot()[1].sandbox.version == 2
    s.settings.sandbox_enabled = False
    fails('CAPABILITY_SCOPE_CHANGED', s.capabilities.validate_execution, s.uid, frozen)
    s.settings.sandbox_enabled = True
    configure(s, enabled=False)
    fails('CAPABILITY_SCOPE_CHANGED', s.capabilities.validate_execution, s.uid, frozen)
    configure(s)
    with s.database.sessions() as session:
        session.get(User, s.uid).role = 'user'
        session.commit()
    fails('CAPABILITY_SCOPE_CHANGED', s.capabilities.validate_execution, s.uid, frozen)


def test_catalog_exposes_reviewed_version_and_resource_boundary(sandbox):
    s = sandbox
    catalog = s.capabilities.public_catalog(s.uid)
    tool = next(item for item in catalog['tools'] if item['name'] == 'sandbox_execute')
    assert tool['requires_approval'] and not tool['idempotent'] and tool['available']
    assert tool['version'] == 1 and tool['required_roles'] == ['admin']
    assert tool['network_policy'] == 'none' and tool['resource_scope'] == 'isolated-workspace'
    assert tool['sandbox_limits'] == SandboxLimits().model_dump(mode='json')
    assert catalog['skills'][0]['approval_tools'] == ['sandbox_execute']
    reader = s.capabilities.public_catalog(s.other)
    assert not reader['skills'][0]['available']
    s.settings.sandbox_enabled = False
    assert not s.capabilities.public_catalog(s.uid)['skills'][0]['available']


def test_api_policy_validation_and_revision_conflict(control):
    s = control
    s.close = lambda: None
    app = create_app(s.settings, s)
    app.dependency_overrides[current_user] = lambda: s.uid
    with TestClient(app) as client:
        path = '/api/v2/capability-center/runtime/sandbox'
        body = {'expected_revision': 1, 'declaration': policy().model_dump(mode='json')}
        assert client.put(path, json={**body, 'declaration': {'enabled': True, 'image': 'latest'}}).status_code == 422
        created = client.put(path, json=body)
        assert created.status_code == 200 and created.json()['catalog']['sandbox']['version'] == 1
        assert client.put(path, json=body).status_code == 409
        app.dependency_overrides[current_user] = lambda: s.other
        assert client.put(path, json={**body, 'expected_revision': 2}).status_code == 403


def test_disabled_feature_preserves_worker_hash_and_preflight_fails_incomplete_setup(control):
    s = control
    registry = WorkerRegistry(s.settings, s.database, kind='run')
    old = registry.configuration_hash()
    s.settings.sandbox_require_rootless = False
    assert registry.configuration_hash() == old
    s.settings.sandbox_enabled = True
    assert registry.configuration_hash() != old
    assert capability_preflight(s.settings, s.database)[1] == 'SANDBOX_CONFIGURATION_REQUIRED'
    s.settings.tool_approvals_enabled = True
    assert capability_preflight(s.settings, s.database)[1] == 'SANDBOX_POLICY_REQUIRED'
    configure(s)
    assert capability_preflight(s.settings, s.database)[0] == 'pass'
    rootful = registry.configuration_hash()
    s.settings.sandbox_require_rootless = True
    assert registry.configuration_hash() != rootful
