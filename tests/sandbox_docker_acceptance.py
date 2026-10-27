"""Explicit Docker acceptance, run inside the trusted worker with no app secrets.

Pipe this file to `python -` in the run-worker image with SANDBOX_TEST_IMAGE set
to a prebuilt immutable workload image. This is intentionally not a host test.
"""
import json
import os
from time import monotonic
from uuid import uuid4

from app.core.errors import AppError
from app.domain.sandbox import SandboxExecution, SandboxLimits, SandboxRequest
from app.domain.tool_approvals import RunToolApproval
from app.infrastructure.sandbox import DockerSandbox


def main():
    image = os.environ['SANDBOX_TEST_IMAGE']
    # Desktop is an explicit development-only acceptance environment. Production
    # keeps require_rootless=True; prove that default rejects this daemon below.
    driver = DockerSandbox(image, require_rootless=False)
    readiness = driver.readiness()
    results = []
    if not readiness['rootless']:
        try:
            DockerSandbox(image).readiness()
        except AppError as error:
            assert error.code == 'SANDBOX_ROOTLESS_REQUIRED'
        else:
            raise AssertionError('Non-rootless daemon accepted by production default')
        results.append({'case': 'production_rootless_required', 'passed': True})

    def run(name, source, *, language='python', expected=None, output=None,
            files=None, cancel=False, cancel_code='RUN_CANCEL_REQUESTED', **limits):
        identity = dict(user_id=uuid4(), session_id=uuid4(), run_id=uuid4(),
                        organization_id=None, catalog_hash='a'*64)
        grant = RunToolApproval(**identity, tool_names=('sandbox_execute',))
        spec = SandboxExecution(invocation_id=uuid4(), run_id=identity['run_id'],
            request=SandboxRequest(language=language, source=source), image=image,
            limits=SandboxLimits(**limits))
        command = driver._command
        started = None

        def checked_command(args, **kwargs):
            nonlocal started
            if args[0] == 'start':
                config = driver._json(['container', 'inspect', spec.container_name],
                                     deadline=monotonic()+10)[0]
                host = config['HostConfig']
                assert host['NetworkMode'] == 'none' and host['ReadonlyRootfs']
                assert host['PidMode'] == '' and host['IpcMode'] == 'none'
                assert not host['Privileged'] and not host.get('Binds')
                assert not host.get('Mounts') and host['CapDrop'] == ['ALL']
                assert 'no-new-privileges:true' in host['SecurityOpt']
                assert host['Memory'] == host['MemorySwap'] == spec.limits.memory_bytes
                assert host['PidsLimit'] == spec.limits.pids_limit
                assert host['NanoCpus'] == round(spec.limits.cpu_count*1_000_000_000)
                assert set(host['Tmpfs']) == {'/tmp', '/workspace'}
                assert config['Config']['User'] == '65532:65532'
                assert config['Config']['WorkingDir'] == '/workspace'
                started = monotonic()
            return command(args, **kwargs)

        def check():
            if cancel and started is not None and monotonic()-started > .5:
                raise AppError(cancel_code, 'Acceptance interruption', 409)

        driver._command = checked_command
        try:
            result = driver.execute(spec, approval=grant, identity=identity,
                                    deadline=monotonic()+40, check=check)
            assert result.error_code == expected, (name, result)
            assert result.success == (expected is None), (name, result)
            if output is not None:
                assert result.stdout.strip() == output, name
            if files is not None:
                assert {file.name: file.content for file in result.files} == files, name
            if not result.success:
                assert not result.files, name
        except AppError as error:
            assert expected is not None and error.code == expected, (name, error.code)
        finally:
            driver._command = command
            code, remaining, _ = command(['ps', '-a', '--filter',
                'name=^/'+spec.container_name+'$', '--format', '{{.ID}}'],
                deadline=monotonic()+10)
            assert code == 0 and not remaining.strip(), ('Container leaked', name)
        results.append({'case': name, 'passed': True})

    run('python_artifact', "from pathlib import Path\nprint('isolated-ok')\nPath('answer.txt').write_bytes(b'42')",
        output='isolated-ok', files={'answer.txt': b'42'})
    run('shell_artifact', "printf 'shell-ok\\n'; mkdir nested; printf 42 > nested/answer.txt",
        language='sh', output='shell-ok', files={'nested/answer.txt': b'42'})
    run('identity_network_root_and_secret_boundary', '''import os, socket
from pathlib import Path
assert os.getuid() == 65532
assert os.getppid() == 1
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert status['Seccomp'].strip() == '2'
assert not any(key in os.environ for key in ('DATABASE_URL','OPENAI_API_KEY','DEEPSEEK_API_KEY','DOCKER_HOST','SANDBOX_TEST_IMAGE'))
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/app/.env')
assert not os.path.exists('/app/data')
try:
    open('/cannot-write-root', 'w')
except OSError:
    pass
else:
    raise AssertionError('Writable root')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=.2)
except OSError:
    pass
else:
    raise AssertionError('Network accessible')
print('boundary-ok')''', output='boundary-ok', files={})
    run('child_json_does_not_override_failure', "import os\nos.write(1, b'{\"success\":true}')\nraise RuntimeError('failed')",
        expected='SANDBOX_EXECUTION_FAILED')
    run('output_flood', "import os\nwhile True: os.write(1,b'x'*8192)",
        output_bytes=1024, expected='SANDBOX_OUTPUT_LIMIT')
    run('timeout', 'import time\ntime.sleep(30)', timeout_seconds=.3,
        expected='SANDBOX_TIMEOUT')
    run('cancel', 'import time\ntime.sleep(30)', cancel=True,
        expected='RUN_CANCEL_REQUESTED')
    run('lease_loss', 'import time\ntime.sleep(30)', cancel=True,
        cancel_code='RUN_LEASE_LOST', expected='RUN_LEASE_LOST')
    run('detached_descendant_cleanup', '''import subprocess
subprocess.Popen(['/usr/local/bin/python','-c','import time;time.sleep(30)'], start_new_session=True)
print('parent-finished')''', output='parent-finished', files={})
    run('symlink_rejected', "import os\nos.symlink('/etc/passwd','leak.txt')",
        expected='SANDBOX_UNSAFE_FILE')
    run('hardlink_rejected', "import os\nopen('a','w').write('x')\nos.link('a','b')",
        expected='SANDBOX_UNSAFE_FILE')
    run('fifo_rejected_without_blocking', "import os\nos.mkfifo('pipe')",
        expected='SANDBOX_UNSAFE_FILE')
    run('unsafe_filename_rejected', "open('..private','w').write('x')",
        expected='SANDBOX_UNSAFE_FILE')
    run('deep_directory_rejected', "from pathlib import Path\nPath('a/b/c/d/e/f').mkdir(parents=True)",
        expected='SANDBOX_FILE_LIMIT')
    run('file_count_limit', "from pathlib import Path\nfor i in range(3): Path(str(i)).write_text('x')",
        max_files=2, expected='SANDBOX_FILE_LIMIT')
    run('aggregate_file_limit', "from pathlib import Path\nfor i in range(2): Path(str(i)).write_bytes(b'x'*1024)",
        max_file_bytes=1024, max_artifact_bytes=1024, expected='SANDBOX_FILE_LIMIT')
    run('single_file_kernel_limit', "with open('large','wb') as target: target.write(b'x'*4096)",
        max_file_bytes=1024, expected='SANDBOX_EXECUTION_FAILED')
    run('memory_limit', "value = bytearray(512*1024*1024)", memory_bytes=67108864,
        expected='SANDBOX_MEMORY_LIMIT')
    run('process_limit', '''import os, time
children = []
try:
    for i in range(100):
        pid = os.fork()
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
except OSError:
    assert len(children) < 100
    print('processes-bounded')
else:
    raise AssertionError('No process limit')''', pids_limit=12, output='processes-bounded', files={})
    print(json.dumps({'schema_version': 1, 'image_id': readiness['image_id'],
        'rootless': readiness['rootless'], 'development_only': not readiness['rootless'],
        'cases': results, 'passed': len(results)}, indent=2))


if __name__ == '__main__':
    main()
