from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.application.artifacts import ArtifactService
from app.application.run_execution import RunWorker
from app.application.sandbox import SandboxService
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.domain.sandbox import SandboxFile, SandboxResult
from app.infrastructure.persistence.models import ChatRun, ChatRunOperation
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, call, final
from test_capability_workflow import expire_lease
from test_sandbox_capabilities import sandbox, control, system


@pytest.fixture
def pipeline(sandbox, monkeypatch):
    s = sandbox
    s.artifacts = ArtifactService(s.store, s.capabilities)
    value = SimpleNamespace(system=s, calls=[], error=None, cancel=False,
        result=SandboxResult(True, None, 0, 5, '42', '', (SandboxFile('answer.txt', b'42'),)))

    class Driver:
        def __init__(self, image, *, require_rootless, namespace):
            assert image == 'sha256:'+'b'*64 and require_rootless
            assert len(namespace) == 64

        def execute(self, invocation, *, approval, identity, deadline, check):
            check()
            assert str(identity['run_id']) == approval['run_id']
            assert invocation.run_id == identity['run_id']
            value.calls.append(invocation)
            if value.cancel:
                s.store.request_cancel(s.uid, invocation.run_id)
            if value.error is not None:
                raise value.error
            check()
            return value.result

    monkeypatch.setattr('app.application.sandbox.DockerSandbox', Driver)
    value.model = ScriptedModel(responses=[call('sandbox_execute', language='python', source='print(6 * 7)'), final('42[1]')])
    value.runtime = SandboxService(s.settings, s.capabilities, s.artifacts)
    s.chat.agent_workflow = SupervisorWorkflow(s.settings, None, Models(value.model), None,
        capabilities=s.capabilities, sandbox=value.runtime)
    monkeypatch.setattr('app.infrastructure.llms.providers.ModelRouter', lambda *args: Models(value.model))
    value.run = s.runs.start(s.uid, s.sid, AgentChatInput(question='calculate 6 * 7', skill_name='compute',
        approved_tools=('sandbox_execute',), approval_catalog_revision=3), 'sandbox-workflow', uuid4())
    return value


def test_actual_supervisor_run_operation_produces_citation_and_committed_artifact(pipeline):
    p, s = pipeline, pipeline.system
    assert RunWorker(s.store, s.chat).run_once()
    run, events = s.store.read(s.uid, p.run.id)
    assert run.state == 'succeeded', [(e.event, e.payload.get('code'), e.payload.get('message')) for e in events]
    assert len(p.calls) == 1

    assert 'sandbox_execute' in p.model.bound_tools[0]
    assert events[-1].payload['citations'][0]['tool_name'] == 'sandbox_execute'
    assert events[-1].payload['citations'][0]['tool_version'] == 1
    artifact = s.artifacts.list(s.uid, p.run.id)[0]
    assert s.artifacts.read(s.uid, p.run.id, artifact['id'])[1] == b'42'
    assert len([e for e in events if e.event == 'artifacts_ready']) == 1
    with s.database.sessions() as session:
        receipt = session.scalar(select(ChatRunOperation).where(ChatRunOperation.run_id == run.id,
            ChatRunOperation.readonly.is_(False)))
        assert receipt.state == 'succeeded' and receipt.attempts == 1
    assert not RunWorker(s.store, s.chat).run_once()
    assert len(p.calls) == 1


def test_successful_sandbox_receipt_replays_on_takeover_without_new_container(pipeline):
    p, s = pipeline, pipeline.system
    p.model.responses = [call('sandbox_execute', language='python', source='print(42)'),
        AppError('RUN_LEASE_LOST', 'test lease lost before next model result', 409), final('42[1]')]
    with pytest.raises(AppError) as error:
        s.chat.execute_prepared(s.store, s.store.claim_next())
    assert error.value.code == 'RUN_LEASE_LOST' and len(p.calls) == 1
    original = s.artifacts.list(s.uid, p.run.id)
    expire_lease(s, p.run.id)
    message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert message.content == '42[1]' and len(p.calls) == 1
    assert s.artifacts.list(s.uid, p.run.id) == original


def test_unknown_execution_cannot_be_automatically_repeated_by_new_owner(pipeline):
    p, s = pipeline, pipeline.system
    p.error = OSError('private adapter error')
    for attempt in range(2):
        if attempt:
            expire_lease(s, p.run.id)
        with pytest.raises(AppError) as error:
            s.chat.execute_prepared(s.store, s.store.claim_next())
        assert error.value.code == 'TOOL_OUTCOME_UNCERTAIN'
    assert len(p.calls) == 1
    assert s.artifacts.list(s.uid, p.run.id) == []
    with s.database.sessions() as session:
        row = session.scalar(select(ChatRunOperation).where(ChatRunOperation.run_id == p.run.id,
            ChatRunOperation.readonly.is_(False)))
        assert row.state == 'uncertain' and row.attempts == 1


def test_cleanup_failure_during_cancel_remains_explicit_and_stops_the_run(pipeline):
    p, s = pipeline, pipeline.system
    p.cancel, p.error = True, AppError('SANDBOX_CLEANUP_FAILED', '隔离容器未能确认清理', 503)
    assert RunWorker(s.store, s.chat).run_once()
    run, events = s.store.read(s.uid, p.run.id)
    assert run.state == 'failed'
    assert events[-1].event == 'error' and events[-1].payload['code'] == 'SANDBOX_CLEANUP_FAILED'
    assert s.artifacts.list(s.uid, p.run.id) == []
    assert len(p.calls) == 1


def test_failure_and_invalid_source_never_publish_evidence_or_artifacts(pipeline):
    p, s = pipeline, pipeline.system
    p.result = SandboxResult(False, 'SANDBOX_TIMEOUT', -9, 200, 'partial', '', ())
    p.model.responses = [call('sandbox_execute', language='python', source='print(42)'), final('执行未完成', kind='insufficient')]
    assert RunWorker(s.store, s.chat).run_once()
    run, events = s.store.read(s.uid, p.run.id)
    assert run.state == 'succeeded' and events[-1].payload['citations'] == []
    assert not s.artifacts.list(s.uid, p.run.id)
    assert not any(e.event == 'artifacts_ready' for e in events)


def test_invalid_language_is_rejected_before_docker_and_does_not_mark_outcome_unknown(pipeline):
    p, s = pipeline, pipeline.system
    p.model.responses = [call('sandbox_execute', language='powershell', source='Write-Output 42'), final('语言不可用', kind='insufficient')]
    assert RunWorker(s.store, s.chat).run_once()
    run, events = s.store.read(s.uid, p.run.id)
    assert run.state == 'succeeded' and not p.calls, [(e.event, e.payload.get('message')) for e in events]
    with s.database.sessions() as session:
        row = session.scalar(select(ChatRunOperation).where(ChatRunOperation.run_id == p.run.id,
            ChatRunOperation.readonly.is_(False)))
        assert row.state == 'succeeded' and 'SANDBOX_INVALID_REQUEST' in str(row.result)


@pytest.mark.parametrize('discovery,arguments', [('search_tools',{'query':'sandbox'}),('describe_skill',{'name':'compute'})])
def test_discovery_and_execution_in_one_parallel_model_batch_do_not_deadlock(pipeline,discovery,arguments):
    from langchain_core.messages import AIMessage
    p,s=pipeline,pipeline.system
    p.model.responses=[AIMessage(content='',tool_calls=[
        {'name':discovery,'args':arguments,'id':'discovery','type':'tool_call'},
        {'name':'sandbox_execute','args':{'language':'python','source':'print(42)'},'id':'execute','type':'tool_call'}]),final('42[1]')]
    with s.database.sessions() as session,session.begin():
        session.get(ChatRun,p.run.id).remaining_seconds=5
    assert RunWorker(s.store,s.chat).run_once()
    run,events=s.store.read(s.uid,p.run.id)
    assert run.state=='succeeded',[(event.event,event.payload.get('code')) for event in events]
    assert len(p.calls)==1 and len(s.artifacts.list(s.uid,p.run.id))==1
