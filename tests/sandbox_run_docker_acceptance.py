"""Run in the trusted Docker-capable worker; uses a disposable PostgreSQL DB.

The LLM is scripted. Docker, migrations, Run operations, blobs and HTTP artifact
routes are real. No business environment or model credentials are needed.
"""
import hashlib
from io import BytesIO
import json
import os
import subprocess
import sys
from datetime import timedelta
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from app.api.dependencies import current_user
from app.application.artifacts import ArtifactService
from app.application.capabilities import CapabilityService
from app.application.chat import ChatService
from app.application.run_execution import RunWorker
from app.application.runs import RunService
from app.application.sandbox import SandboxService
from app.application.sessions import SessionService
from app.core.config import Settings
from app.domain.capabilities import SandboxPolicy, SkillDeclaration
from app.domain.contracts import AgentChatInput
from app.domain.sandbox import SandboxExecution, SandboxLimits, SandboxRequest
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import ChatRun, ChatRunOperation, User
from app.infrastructure.sandbox import DockerSandbox
from app.main import create_app
from app.migrate import upgrade
from app.workflows.supervisor import SupervisorWorkflow


class ScriptedModel(BaseChatModel):
    responses: list[Any]
    position: int = 0

    @property
    def _llm_type(self):
        return 'sandbox-acceptance-scripted'

    def bind_tools(self, tools, **kwargs):
        assert 'sandbox_execute' in [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        value = self.responses[self.position]
        self.position += 1
        return ChatResult(generations=[ChatGeneration(message=value)])


class Models:
    def __init__(self, model):
        self.model = model

    def agent_model(self, provider, timeout_seconds):
        return self.model, provider, 'sandbox-acceptance-scripted'

    def agent_fallback(self, provider, timeout_seconds):
        return None


def model(source):
    return ScriptedModel(responses=[AIMessage(content='', tool_calls=[{
        'name': 'sandbox_execute', 'args': {'language': 'python', 'source': source},
        'id': str(uuid4()), 'type': 'tool_call'}]),
        AIMessage(content=json.dumps({'kind': 'grounded', 'answer': '42[1]'}))])


def verify_worker_crash_cleanup(sandbox, runs, artifacts, capabilities, owner, parent, observer):
    """SIGKILL the trusted driver, retain a live lease, then expire it and reap."""
    run = runs.start(owner.id, parent.id, AgentChatInput(question='worker crash probe', skill_name='compute',
        approved_tools=('sandbox_execute',), approval_catalog_revision=capabilities.snapshot()[0]), 'orphan-probe', uuid4())
    claim = runs.store.claim_next(run.id)
    key = 'tool/orphan-probe'
    runs.store.begin_operation(claim, key, {'name':'sandbox_execute'}, readonly=False)
    execution, grant, identity = artifacts.prepare_execution(claim, key)
    image = execution.catalog.sandbox.declaration.image
    invocation = SandboxExecution(invocation_id=uuid4(), run_id=run.id, image=image,
        limits=SandboxLimits(), request=SandboxRequest(language='python', source='import time\ntime.sleep(30)'))
    payload = json.dumps({'execution':invocation.model_dump(mode='json'),'approval':grant,
        'namespace':sandbox.namespace,'identity':{k:str(v) if v is not None else None for k,v in identity.items()}})
    child_code = '''import json,sys
from time import monotonic
from uuid import UUID
from app.domain.sandbox import SandboxExecution
from app.infrastructure.sandbox import DockerSandbox
data=json.load(sys.stdin)
identity={k:UUID(v) if k.endswith('_id') and v is not None else v for k,v in data['identity'].items()}
spec=SandboxExecution.model_validate_json(json.dumps(data['execution']))
DockerSandbox(spec.image,require_rootless=False,namespace=data['namespace']).execute(spec,
    approval=data['approval'],identity=identity,deadline=monotonic()+40)
'''
    process = subprocess.Popen([sys.executable,'-c',child_code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    identifier = None
    foreign = None
    try:
        process.stdin.write(payload.encode());process.stdin.close()
        deadline = monotonic()+15
        while monotonic()<deadline and process.poll() is None:
            code, output, _ = observer._command(['ps','--no-trunc','--filter','name=^/'+invocation.container_name+'$',
                '--format','{{.ID}}'],deadline=monotonic()+5)
            if code==0 and output.strip():
                identifier=output.decode().strip();break
            sleep(.1)
        assert identifier, 'Crash probe never observed a real running workload'
        process.kill();process.wait(timeout=5)
        # An otherwise identical container in another deployment must survive.
        foreign_spec = invocation.model_copy(update={'invocation_id':uuid4()})
        foreign_driver = DockerSandbox(image,require_rootless=False,namespace='f'*64)
        code, output, _ = foreign_driver._command(foreign_driver.create_args(foreign_spec,uuid4().hex),deadline=monotonic()+10)
        assert code==0
        foreign=output.decode().strip()
        with runs.store.database.sessions() as session, session.begin():
            row=session.get(ChatRun,run.id)
            row.lease_expires_at=runs.store.now(session)+timedelta(seconds=300)
        print('Worker killed; waiting for the real 90-second orphan grace period',flush=True)
        sleep(92)
        scoped = DockerSandbox(image,require_rootless=False,namespace=sandbox.namespace)
        assert scoped.reap(sandbox.cleanup_guard)==0, 'Live lease must protect even an old orphan'
        with runs.store.database.sessions() as session, session.begin():
            row=session.get(ChatRun,run.id)
            row.lease_expires_at=runs.store.now(session)-timedelta(seconds=1)
        assert scoped.reap(sandbox.cleanup_guard)==1
        code, output, _=observer._command(['ps','-a','--no-trunc','--filter','name=^/'+invocation.container_name+'$',
            '--format','{{.ID}}'],deadline=monotonic()+5)
        assert code==0 and not output.strip()
        code, _, _=observer._command(['container','inspect',foreign],deadline=monotonic()+5)
        assert code==0, 'Another deployment must not be reaped'
        with runs.store.database.sessions() as session:
            assert session.get(ChatRunOperation,(run.id,key)).state=='started'
        return {'actual_driver_sigkill':True,'real_age_grace_seconds':90,'live_lease_protected':True,
            'expired_orphan_removed':True,'foreign_deployment_preserved':True,'unknown_receipt_preserved':True}
    finally:
        if process.poll() is None:process.kill()
        process.wait(timeout=5)
        process.stdout.close();process.stderr.close()
        # Only the two exact disposable IDs created above may be removed here.
        for value in (identifier,foreign):
            if value:observer._command(['rm','--force',value],deadline=monotonic()+8)


def main():
    base = make_url(os.environ['SANDBOX_TEST_DATABASE_URL'])
    assert 'test' in (base.database or '').lower()
    name = 'parity_test_stage8_sandbox_run_'+uuid4().hex
    admin = create_engine(base, isolation_level='AUTOCOMMIT', hide_parameters=True)
    db, runs = None, None
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        settings = Settings(_env_file=None, database_url=base.set(database=name).render_as_string(hide_password=False),
            database_require_tls=False, chat_runs_enabled=True, capability_center_enabled=True,
            tool_approvals_enabled=True, sandbox_enabled=True, sandbox_require_rootless=False)
        db = Database(settings)
        upgrade(db)
        with db.sessions() as session:
            owner = User(username='sandbox-owner', password_hash='fixture', role='admin')
            reader = User(username='sandbox-reader', password_hash='fixture', role='user')
            session.add_all([owner, reader]); session.commit()
        capabilities = CapabilityService(settings, db)
        image = os.environ['SANDBOX_TEST_IMAGE']
        capabilities.update_runtime(owner.id, 'sandbox', SandboxPolicy(enabled=True, image=image), 1)
        capabilities.change(owner.id, 'skills', 'compute', expected_revision=2, create=True,
            declaration=SkillDeclaration(name='compute', description='Compute', instructions='Execute then cite actual results.',
                                         allowed_tools=('sandbox_execute',)))
        sessions = SessionService(db)
        parent = sessions.create(owner.id, 'real-sandbox-acceptance')
        chat = ChatService(settings, db, sessions, SimpleNamespace())
        runs = RunService(settings, db, chat)
        artifacts = ArtifactService(runs.store, capabilities)
        sandbox = SandboxService(settings, capabilities, artifacts)
        readiness = sandbox.readiness()
        observer = DockerSandbox(image, require_rootless=False)
        source = '''from pathlib import Path
import struct, zlib
Path('answer.txt').write_text('42')
Path('active.html').write_text('<script>alert(1)</script>')
def chunk(kind, data):
    return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
png = b'\\x89PNG\\r\\n\\x1a\\n'+chunk(b'IHDR',struct.pack('!2I5B',2,2,8,6,0,0,0))
png += chunk(b'IDAT',zlib.compress((b'\\x00'+b'\\x00\\xff\\x00\\xff'*2)*2))+chunk(b'IEND',b'')
Path('chart.png').write_bytes(png)
print(42)'''
        active_model = [model(source)]
        workflow = SupervisorWorkflow(settings, None, Models(active_model[0]), None,
                                      capabilities=capabilities, sandbox=sandbox)
        chat.agent_workflow = workflow
        with patch('app.infrastructure.llms.providers.ModelRouter', side_effect=lambda *args: Models(active_model[0])):
            request = AgentChatInput(question='calculate', skill_name='compute',
                                    approved_tools=('sandbox_execute',), approval_catalog_revision=3)
            run = runs.start(owner.id, parent.id, request, 'real-sandbox-acceptance', uuid4())
            # Updating current policy must not replace the reviewed v1 snapshot.
            capabilities.update_runtime(owner.id, 'sandbox', SandboxPolicy(enabled=True, image=image), 3)
            assert RunWorker(runs.store, chat).run_once()
            completed, events = runs.store.read(owner.id, run.id)
            assert completed.state == 'succeeded', [(e.event, e.payload.get('code')) for e in events]
            assert events[-1].payload['citations'][0]['tool_name'] == 'sandbox_execute'
            assert len([e for e in events if e.event == 'artifacts_ready']) == 1
            records = artifacts.list(owner.id, run.id)
            assert {row['name'] for row in records} == {'answer.txt', 'active.html', 'chart.png'}
            assert all(row['tool_version'] == 1 for row in records)
            services = SimpleNamespace(runs=runs, artifacts=artifacts, close=lambda: None)
            app = create_app(settings, services)
            app.dependency_overrides[current_user] = lambda: owner.id
            with TestClient(app) as client:
                path = f'/api/v2/runs/{run.id}/artifacts'
                assert len(client.get(path).json()['items']) == 3
                for row in records:
                    response = client.get(f"{path}/{row['id']}/download")
                    assert response.status_code == 200
                    assert hashlib.sha256(response.content).hexdigest() == row['sha256']
                    assert response.headers['content-disposition'].startswith('attachment;')
                    preview = client.get(f"{path}/{row['id']}/preview")
                    if row['name'] == 'chart.png':
                        assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
                        with Image.open(BytesIO(preview.content)) as rendered:
                            assert rendered.size == (2, 2)
                    elif row['name'] == 'active.html':
                        assert preview.status_code == 415
                    else:
                        assert preview.status_code == 200 and preview.content == b'42'
                app.dependency_overrides[current_user] = lambda: reader.id
                assert client.get(f"{path}/{records[0]['id']}/download").status_code == 404
                app.dependency_overrides.clear()
                assert client.get(path).status_code == 401
            assert not RunWorker(runs.store, chat).run_once()
            # A second real Run is cancelled while its workload container runs.
            active_model[0] = model('import time\ntime.sleep(30)')
            cancel_run = runs.start(owner.id, parent.id, AgentChatInput(question='cancel probe', skill_name='compute',
                approved_tools=('sandbox_execute',), approval_catalog_revision=4), 'real-sandbox-cancel', uuid4())
            thread = Thread(target=RunWorker(runs.store, chat).run_once)
            thread.start()
            deadline, found = monotonic()+25, False
            while thread.is_alive() and monotonic() < deadline:
                code, output, _ = observer._command(['ps', '--filter',
                    'label=learningqa.sandbox.run='+str(cancel_run.id), '--format', '{{.ID}}'], deadline=monotonic()+5)
                if code == 0 and output.strip():
                    found = True
                    break
                sleep(.1)
            assert found, 'Cancellation probe never observed a running workload'
            runs.store.request_cancel(owner.id, cancel_run.id)
            thread.join(timeout=20)
            assert not thread.is_alive()
            cancelled, _ = runs.store.read(owner.id, cancel_run.id)
            assert cancelled.state == 'cancelled'
            assert not artifacts.list(owner.id, cancel_run.id)
            for rid in (run.id, cancel_run.id):
                code, remaining, _ = observer._command(['ps', '-a', '--filter',
                    'label=learningqa.sandbox.run='+str(rid), '--format', '{{.ID}}'], deadline=monotonic()+5)
                assert code == 0 and not remaining.strip()
            with db.sessions() as session:
                receipts = list(session.scalars(select(ChatRunOperation).where(
                    ChatRunOperation.run_id == run.id, ChatRunOperation.readonly.is_(False))))
                assert len(receipts) == 1 and receipts[0].state == 'succeeded' and receipts[0].attempts == 1
        reaping = verify_worker_crash_cleanup(sandbox,runs,artifacts,capabilities,owner,parent,observer)
        print(json.dumps({'schema_version': 1, 'real_docker': True, 'real_postgresql': True,
            'model': 'scripted-no-provider-calls', 'api': 'in-process-http-testclient',
            'rootless': readiness['rootless'], 'development_only': True, 'artifacts': len(records),
            'frozen_tool_version': 1, 'current_tool_version': 2, 'download_hashes_match': True,
            'safe_previews': True, 'cross_user_rejected': True, 'unauthenticated_rejected': True,
            'running_container_cancelled': True, 'remaining_run_containers': 0,
            'workload_image_id': readiness['image_id'], 'worker_crash_cleanup':reaping}, indent=2))
    finally:
        if runs is not None:
            runs.close()
        if db is not None:
            db.close()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == '__main__':
    main()
