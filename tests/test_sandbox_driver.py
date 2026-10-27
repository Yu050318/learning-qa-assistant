import base64
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tarfile
import threading
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import AppError
from app.domain.sandbox import SandboxExecution, SandboxLimits, SandboxRequest
from app.domain.tool_approvals import RunToolApproval
from app.infrastructure.sandbox import DockerSandbox, decode_result, MANAGED_LABEL
from app.infrastructure.sandbox_runner import read_request, RunnerFailure, terminate_descendants


def protocol(**updates):
    return {"schema_version": 1, "success": True, "error_code": None, "exit_code": 0,
        "stdout_b64": base64.b64encode(b'42').decode(), "stderr_b64": "", "duration_ms": 1, "files": [], **updates}


def fails(code, function, *args, **kwargs):
    with pytest.raises(AppError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def spec():
    identity = dict(user_id=uuid4(), session_id=uuid4(), run_id=uuid4(), organization_id=None, catalog_hash='a'*64)
    grant = RunToolApproval(**identity, tool_names=('sandbox_execute',))
    execution = SandboxExecution(invocation_id=uuid4(), run_id=identity['run_id'], image='sha256:'+'b'*64,
        request=SandboxRequest(language='python', source="print('--privileged --network host')"), limits=SandboxLimits())
    return execution, grant, identity


@pytest.fixture
def driver():
    execution, grant, identity = spec()
    client = object.__new__(DockerSandbox)
    client.image, client.slots = execution.image, threading.BoundedSemaphore(2)
    client.readiness = lambda **kw: {'ready': True}
    value = SimpleNamespace(client=client, execution=execution, grant=grant, identity=identity, calls=[], mismatch=False,
        foreign=False, cleanup_fail=False, cancel=False)
    def command(args, input_bytes=None, **kwargs):
        value.calls.append((args, input_bytes))
        if args[0] == 'create':
            value.owner = next(a.split('=',1)[1] for a in args if a.startswith('learningqa.sandbox.owner='))
            return 0, b'c'*64+b'\n', b''
        if args[0] == 'cp':
            content = b'wrong runner' if value.mismatch else Path('app/infrastructure/sandbox_runner.py').read_bytes()
            buffer = BytesIO()
            with tarfile.open(fileobj=buffer, mode='w:') as archive:
                member = tarfile.TarInfo('runner.py');member.size=len(content)
                archive.addfile(member, BytesIO(content))
            return 0, buffer.getvalue(), b''
        if args[0] == 'start':
            if value.cancel:
                raise AppError('RUN_CANCEL_REQUESTED', 'fixture', 409)
            return 0, json.dumps(protocol()).encode(), b''
        if args[:2] == ['container','inspect']:
            if args[-1] == '{{json .State}}':
                return 0, b'{"OOMKilled":false}', b''
            labels={MANAGED_LABEL:'true','learningqa.sandbox.owner':'foreign' if value.foreign else value.owner}
            return 0, json.dumps(labels).encode(), b''
        if args[0] == 'rm':
            return (1 if value.cleanup_fail else 0), b'', b''
        raise AssertionError(args)
    client._command = command
    return value


def execute(driver, **updates):
    return driver.client.execute(driver.execution, **{'approval':driver.grant,'identity':driver.identity,'deadline':monotonic()+30,**updates})


def test_source_is_only_stdin_and_creation_has_fixed_isolation_without_mounts(driver):
    result = execute(driver)
    assert result.success and result.stdout == '42'
    create = driver.calls[0][0]
    assert create[create.index('--network')+1] == 'none'
    assert create[create.index('--user')+1] == '65532:65532'
    assert '--read-only' in create and '--pull=never' in create and '--no-healthcheck' in create
    assert '--privileged' not in create and '--mount' not in create and '-v' not in create
    assert '--pid' not in create  # Docker's default is a private PID namespace.
    assert driver.execution.request.source not in create
    start = next(input for args,input in driver.calls if args[0]=='start')
    assert json.loads(start)['source'] == driver.execution.request.source
    assert driver.calls[-1][0] == ['rm','--force',driver.execution.container_name]


def test_unapproved_or_wrong_run_never_accesses_docker(driver):
    fails('SANDBOX_APPROVAL_REQUIRED', execute, driver, approval=None)
    fails('SANDBOX_APPROVAL_REQUIRED', execute, driver, identity={**driver.identity,'run_id':uuid4()})
    assert not driver.calls


def test_runner_mismatch_and_cancellation_clean_up_without_repeating_code(driver):
    driver.mismatch=True
    fails('SANDBOX_RUNNER_MISMATCH', execute, driver)
    assert not any(args[0]=='start' for args,_ in driver.calls)
    assert driver.calls[-1][0][0]=='rm'
    driver.mismatch=False;driver.cancel=True;driver.calls=[]
    fails('RUN_CANCEL_REQUESTED', execute, driver)
    assert driver.calls[-1][0][0]=='rm'


def test_cleanup_failure_is_explicit_and_another_owner_is_never_removed(driver):
    driver.cleanup_fail=True
    fails('SANDBOX_CLEANUP_FAILED', execute, driver)
    driver.cleanup_fail=False;driver.foreign=True;driver.calls=[]
    execute(driver)
    assert not any(args[0]=='rm' for args,_ in driver.calls)


@pytest.mark.parametrize('updates', [
    {'success':False,'exit_code':1}, {'success':True,'error_code':'SANDBOX_TIMEOUT'},
    {'exit_code':True}, {'schema_version':True}, {'error_code':'private driver error'},
    {'duration_ms':-1}, {'stdout_b64':'not-base64'}, {'files':[{'name':'../secret','content_b64':'','size_bytes':0,'sha256':'x'}]},
])
def test_malformed_or_forged_results_fail_closed(updates):
    fails('SANDBOX_PROTOCOL_ERROR', decode_result, json.dumps(protocol(**updates)), SandboxLimits())


def test_artifact_content_size_and_digest_are_checked():
    import hashlib
    file={'name':'results/answer.txt','size_bytes':2,'sha256':hashlib.sha256(b'42').hexdigest(),'content_b64':'NDI='}
    result=decode_result(json.dumps(protocol(files=[file])),SandboxLimits())
    assert result.files[0].content==b'42' and result.files[0].sha256==file['sha256']
    assert '42' not in repr(result)
    fails('SANDBOX_PROTOCOL_ERROR',decode_result,json.dumps(protocol(files=[file,file])),SandboxLimits())
    fails('SANDBOX_PROTOCOL_ERROR',decode_result,json.dumps(protocol(files=[{**file,'size_bytes':1}])),SandboxLimits())
    fails('SANDBOX_PROTOCOL_ERROR',decode_result,json.dumps(protocol(files=[{**file,'sha256':'a'*64}])),SandboxLimits())


def test_process_transport_bounds_output_and_propagates_control_errors():
    client=object.__new__(DockerSandbox);client.prefix=[sys.executable];client.env={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','WINDIR'}}
    fails('SANDBOX_OUTPUT_LIMIT',client._command,['-c',"print('x'*10000)"],deadline=monotonic()+5,max_output=1024)
    fails('SANDBOX_TIMEOUT',client._command,['-c','import time;time.sleep(10)'],deadline=monotonic()+.1)
    counter=[]
    def cancel():
        counter.append(1)
        if len(counter)>1:raise AppError('RUN_CANCEL_REQUESTED','fixture',409)
    fails('RUN_CANCEL_REQUESTED',client._command,['-c','import time;time.sleep(10)'],deadline=monotonic()+5,check=cancel)


def test_runner_protocol_and_namespace_guard_do_not_execute_on_the_host():
    frame={'schema_version':1,'language':'python','source':'print(1)','limits':SandboxLimits().model_dump(include={'timeout_seconds','output_bytes','max_files','max_file_bytes','max_artifact_bytes'})}
    assert read_request(BytesIO(json.dumps(frame).encode()))['source']=='print(1)'
    with pytest.raises(RunnerFailure):read_request(BytesIO(json.dumps({**frame,'network':'host'}).encode()))
    with pytest.raises(RunnerFailure):terminate_descendants()
