"""Fixed Docker invocation; source is stdin data, never a host shell command."""
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import threading
from time import monotonic, sleep
from uuid import UUID, uuid4

from app.core.errors import AppError
from app.entity.bo.sandbox import SandboxExecution, SandboxFile, SandboxResult, validate_image_reference
from app.entity.bo.tool_approvals import RunToolApproval


ENTRYPOINT = ["/usr/local/bin/python", "-I", "-B", "/opt/learningqa/runner.py"]
MANAGED_LABEL = "learningqa.sandbox.managed"
ERRORS = frozenset({"SANDBOX_INVALID_REQUEST", "SANDBOX_INVALID_CONTEXT", "SANDBOX_PROCESS_LIMIT",
    "SANDBOX_TIMEOUT", "SANDBOX_OUTPUT_LIMIT", "SANDBOX_EXECUTION_FAILED", "SANDBOX_FILE_LIMIT", "SANDBOX_UNSAFE_FILE"})


def sandbox_error(code, status=503):
    return AppError(code, "隔离执行未能完成，请根据错误标识核查执行与清理状态", status)


class DockerSandbox:
    def __init__(self, image, *, require_rootless=True, docker_binary="docker", namespace=None):
        try:
            validate_image_reference(image)
            if type(require_rootless) is not bool:
                raise ValueError()
            if namespace is not None and re.fullmatch(r'[a-f0-9]{64}', namespace) is None:
                raise ValueError()
        except ValueError:
            raise sandbox_error("SANDBOX_IMAGE_INVALID") from None
        self.image, self.require_rootless = image, require_rootless
        self.namespace = namespace
        binary = shutil.which(docker_binary)
        if not binary:
            raise sandbox_error("SANDBOX_ADAPTER_UNAVAILABLE")
        self.prefix = [str(Path(binary).resolve())]
        # Docker's local configuration is needed on Windows; application secrets
        # are excluded even from this trusted CLI process, and never reach a child.
        names = {"PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "TEMP", "TMP"}
        self.env = {name: value for name, value in os.environ.items() if name.upper() in names}
        code, out, _ = self._command(["context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"], deadline=monotonic()+10)
        try:
            host = json.loads(out)
            if code or not isinstance(host, str) or not (host.startswith('unix:///') or re.fullmatch(r'npipe:////\./pipe/[A-Za-z0-9_.-]+', host)):
                raise ValueError()
        except (ValueError, TypeError):
            raise sandbox_error("SANDBOX_LOCAL_DAEMON_REQUIRED") from None
        self.prefix += ['--host', host]
        self.slots = threading.BoundedSemaphore(2)

    def _command(self, args, *, deadline, input_bytes=None, check=lambda: None, max_output=65536):
        check()
        if monotonic() >= deadline:
            raise sandbox_error("SANDBOX_TIMEOUT", 504)
        try:
            process = subprocess.Popen([*self.prefix, *args], stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, shell=False, close_fds=True)
        except (OSError, ValueError):
            raise sandbox_error("SANDBOX_ADAPTER_UNAVAILABLE") from None
        stdout, stderr = bytearray(), bytearray()
        lock, overflow = threading.Lock(), threading.Event()
        def read(pipe, target):
            try:
                while chunk := pipe.read(16384):
                    with lock:
                        remaining = max(0, max_output-len(stdout)-len(stderr))
                        target.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            overflow.set()
            except OSError:
                pass
            finally:
                pipe.close()
        threads = [threading.Thread(target=read, args=(process.stdout, stdout), daemon=True),
                   threading.Thread(target=read, args=(process.stderr, stderr), daemon=True)]
        if input_bytes is not None:
            def write():
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.flush()
                except (OSError, BrokenPipeError):
                    pass
                finally:
                    process.stdin.close()
            threads.append(threading.Thread(target=write, daemon=True))
        for thread in threads:
            thread.start()
        try:
            while process.poll() is None:
                check()
                if overflow.is_set():
                    raise sandbox_error("SANDBOX_OUTPUT_LIMIT", 413)
                if monotonic() >= deadline:
                    raise sandbox_error("SANDBOX_TIMEOUT", 504)
                sleep(.02)
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise sandbox_error("SANDBOX_PROCESS_LIMIT") from None
            for thread in threads:
                thread.join(timeout=1)
        if overflow.is_set() or any(thread.is_alive() for thread in threads):
            raise sandbox_error("SANDBOX_OUTPUT_LIMIT", 413)
        return process.returncode, bytes(stdout), bytes(stderr)

    def _json(self, args, *, deadline, check=lambda: None):
        code, out, _ = self._command(args, deadline=deadline, check=check)
        try:
            if code:
                raise ValueError()
            return json.loads(out)
        except (ValueError, TypeError):
            raise sandbox_error("SANDBOX_NOT_READY") from None

    def readiness(self, *, deadline=None, check=lambda: None):
        deadline = deadline or monotonic()+15
        info = self._json(['info', '--format', '{"security":{{json .SecurityOptions}},"os":{{json .OSType}}}'], deadline=deadline, check=check)
        if not isinstance(info, dict) or info.get('os') != 'linux':
            raise sandbox_error("SANDBOX_LINUX_REQUIRED")
        if not isinstance(info.get('security'), list):
            raise sandbox_error("SANDBOX_NOT_READY")
        rootless = any('rootless' in str(value) for value in info.get('security', []))
        if self.require_rootless and not rootless:
            raise sandbox_error("SANDBOX_ROOTLESS_REQUIRED")
        values = self._json(['image', 'inspect', self.image], deadline=deadline, check=check)
        try:
            image = values[0]
            config = image['Config']
            if not re.fullmatch(r'sha256:[a-f0-9]{64}', image['Id']) or config['Entrypoint'] != ENTRYPOINT or config['User'] != '65532:65532' or config['WorkingDir'] != '/workspace' or config.get('Volumes') or config.get('Cmd'):
                raise ValueError()
            if self.image.startswith('sha256:') and image['Id'] != self.image:
                raise ValueError()
        except (ValueError, KeyError, TypeError, IndexError):
            raise sandbox_error("SANDBOX_IMAGE_INVALID") from None
        return {"ready": True, "image_id": image['Id'], "rootless": rootless}

    def create_args(self, spec, owner):
        limit = spec.limits
        workspace = f'rw,noexec,nosuid,nodev,size={limit.workspace_bytes},nr_inodes=256,mode=0700,uid=65532,gid=65532'
        return ['create', '--pull=never', '--interactive', '--name', spec.container_name,
            *(['--label', 'learningqa.sandbox.scope='+self.namespace] if getattr(self, 'namespace', None) else []),
            '--label', MANAGED_LABEL+'=true', '--label', 'learningqa.sandbox.owner='+owner,
            '--label', 'learningqa.sandbox.run='+str(spec.run_id), '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', str(limit.pids_limit),
            '--memory', str(limit.memory_bytes), '--memory-swap', str(limit.memory_bytes), '--cpus', str(limit.cpu_count),
            '--ulimit', 'core=0:0', '--ulimit', 'nofile=64:64', '--ulimit', f'fsize={limit.max_file_bytes}:{limit.max_file_bytes}',
            '--ipc', 'none', '--user', '65532:65532', '--hostname', 'sandbox', '--workdir', '/workspace',
            '--tmpfs', '/workspace:'+workspace, '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16777216,nr_inodes=128,mode=0700,uid=65532,gid=65532',
            '--log-driver', 'none', '--no-healthcheck', '--stop-timeout', '1',
            '--env', 'HOME=/workspace', '--env', 'LANG=C.UTF-8', '--env', 'LC_ALL=C.UTF-8',
            '--env', 'PATH=/usr/local/bin:/usr/bin:/bin', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONNOUSERSITE=1', spec.image]

    def reap(self, guard):
        """Delete only old, scoped, immutable container IDs while guard holds a Run lock."""
        if not self.namespace:
            raise sandbox_error('SANDBOX_CLEANUP_SCOPE_REQUIRED')
        deadline = monotonic()+20
        self.readiness(deadline=deadline)
        code, out, _ = self._command(['ps', '-a', '--no-trunc', '--filter', 'label='+MANAGED_LABEL+'=true',
            '--filter', 'label=learningqa.sandbox.scope='+self.namespace, '--format', '{{.ID}}'], deadline=deadline)
        identifiers = out.decode('ascii', errors='replace').splitlines()
        # ponytail: bounded scan; alert rather than silently skip a growing cleanup backlog.
        if code or len(identifiers)>64 or any(re.fullmatch(r'[a-f0-9]{64}', value) is None for value in identifiers):
            raise sandbox_error('SANDBOX_CLEANUP_FAILED')
        removed = 0
        for identifier in identifiers:
            code, raw, _ = self._command(['container', 'inspect', identifier], deadline=deadline)
            if code:  # Another Worker may have already cleaned this immutable ID.
                continue
            try:
                item, = json.loads(raw)
                labels = item['Config']['Labels']
                created = datetime.fromisoformat(item['Created'].replace('Z', '+00:00'))
                valid = (item['Id']==identifier and labels.get(MANAGED_LABEL)=='true'
                    and labels.get('learningqa.sandbox.scope')==self.namespace
                    and re.fullmatch(r'/learningqa-sandbox-[a-f0-9]{32}', item['Name'])
                    and re.fullmatch(r'[a-f0-9]{32}', labels.get('learningqa.sandbox.owner', ''))
                    and created.tzinfo is not None)
                run_id = UUID(labels['learningqa.sandbox.run'])
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            # The runner's hard maximum is 30s; include transport and cleanup grace.
            if not valid or (datetime.now(timezone.utc)-created).total_seconds()<90:
                continue
            with guard(run_id) as allowed:
                if allowed:
                    code, _, _ = self._command(['rm', '--force', identifier], deadline=deadline)
                    if code:
                        raise sandbox_error('SANDBOX_CLEANUP_FAILED')
                    removed += 1
        return removed

    def _verify_runner(self, spec, deadline, check):
        code, archive, _ = self._command(['cp', spec.container_name+':/opt/learningqa/runner.py', '-'], deadline=deadline, check=check)
        try:
            expected = Path(__file__).with_name('sandbox_runner.py').read_bytes()
            if code:
                raise ValueError()
            with tarfile.open(fileobj=BytesIO(archive), mode='r:') as tar:
                members = tar.getmembers()
                if len(members) != 1 or not members[0].isreg() or members[0].size != len(expected):
                    raise ValueError()
                if tar.extractfile(members[0]).read(len(expected)+1) != expected:
                    raise ValueError()
        except (OSError, ValueError, tarfile.TarError, AttributeError):
            raise sandbox_error("SANDBOX_RUNNER_MISMATCH") from None

    def _cleanup(self, spec, owner):
        deadline = monotonic()+8
        code, out, _ = self._command(['container', 'inspect', spec.container_name, '--format', '{{json .Config.Labels}}'], deadline=deadline)
        if code:
            code, out, _ = self._command(['ps', '-a', '--filter', 'name=^/'+spec.container_name+'$', '--format', '{{.ID}}'], deadline=deadline)
            if code or out.strip():
                raise sandbox_error("SANDBOX_CLEANUP_FAILED")
            return
        try:
            labels = json.loads(out)
            if labels.get(MANAGED_LABEL) != 'true' or labels.get('learningqa.sandbox.owner') != owner:
                return  # This call must never remove another invocation's container.
        except (ValueError, TypeError, AttributeError):
            raise sandbox_error("SANDBOX_CLEANUP_FAILED") from None
        code, _, _ = self._command(['rm', '--force', spec.container_name], deadline=deadline)
        if code:
            raise sandbox_error("SANDBOX_CLEANUP_FAILED")

    def execute(self, execution, *, approval, identity, deadline, check=lambda: None):
        try:
            spec = SandboxExecution.model_validate_json(execution.model_dump_json(warnings=False))
            grant = RunToolApproval.model_validate_json(approval.model_dump_json(warnings=False) if isinstance(approval, RunToolApproval) else json.dumps(approval))
            if spec.image != self.image or spec.run_id != identity.get('run_id') or not grant.allows('sandbox_execute', **identity):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise sandbox_error("SANDBOX_APPROVAL_REQUIRED", 403) from None
        if not self.slots.acquire(blocking=False):
            raise sandbox_error("SANDBOX_BUSY", 429)
        owner, attempted = uuid4().hex, False
        deadline = min(deadline, monotonic()+spec.limits.timeout_seconds+5)
        try:
            self.readiness(deadline=deadline, check=check)
            check()
            attempted = True
            code, out, _ = self._command(self.create_args(spec, owner), deadline=deadline, check=check)
            if code or not re.fullmatch(rb'[a-f0-9]{64}\s*', out):
                raise sandbox_error("SANDBOX_CREATE_FAILED")
            self._verify_runner(spec, deadline, check)
            limits = spec.limits.model_dump(include={'timeout_seconds', 'output_bytes', 'max_files', 'max_file_bytes', 'max_artifact_bytes'})
            limits['timeout_seconds'] = min(limits['timeout_seconds'], max(.1, deadline-monotonic()-1))
            frame = json.dumps({'schema_version': 1, **spec.request.model_dump(), 'limits': limits}, ensure_ascii=False).encode('utf-8')
            code, out, _ = self._command(['start', '--attach', '--interactive', spec.container_name], deadline=deadline,
                check=check, input_bytes=frame, max_output=6*1024*1024)
            state = self._json(['container', 'inspect', spec.container_name, '--format', '{{json .State}}'], deadline=deadline, check=check)
            if state.get('OOMKilled'):
                raise sandbox_error("SANDBOX_MEMORY_LIMIT")
            if code:
                raise sandbox_error("SANDBOX_EXECUTION_FAILED")
            return decode_result(out, spec.limits)
        finally:
            try:
                if attempted:
                    try:
                        self._cleanup(spec, owner)
                    except AppError:
                        raise sandbox_error("SANDBOX_CLEANUP_FAILED") from None
            finally:
                self.slots.release()


def decode_result(raw, limits):
    try:
        if not isinstance(raw, (bytes, str)) or len(raw) > 6*1024*1024:
            raise ValueError()
        data = json.loads(raw)
        if set(data) != {'schema_version', 'success', 'error_code', 'exit_code', 'stdout_b64', 'stderr_b64', 'duration_ms', 'files'}:
            raise ValueError()
        if type(data['schema_version']) is not int or data['schema_version'] != 1 or type(data['success']) is not bool:
            raise ValueError()
        if data['error_code'] is not None and data['error_code'] not in ERRORS:
            raise ValueError()
        if data['exit_code'] is not None and type(data['exit_code']) is not int or type(data['duration_ms']) is not int or not 0 <= data['duration_ms'] <= 60000:
            raise ValueError()
        if data['success'] != (data['error_code'] is None and data['exit_code'] == 0):
            raise ValueError()
        if not data['success'] and data['error_code'] is None:
            raise ValueError()
        stdout, stderr = (base64.b64decode(data[key], validate=True) for key in ('stdout_b64', 'stderr_b64'))
        if len(stdout)+len(stderr) > limits.output_bytes or not isinstance(data['files'], list) or len(data['files']) > limits.max_files or not data['success'] and data['files']:
            raise ValueError()
        files, total = [], 0
        for item in data['files']:
            if set(item) != {'name', 'size_bytes', 'sha256', 'content_b64'}:
                raise ValueError()
            name = item['name']
            if not isinstance(name, str) or not 1 <= len(name) <= 256 or len(name.split('/')) > 5 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', segment) for segment in name.split('/')):
                raise ValueError()
            content = base64.b64decode(item['content_b64'], validate=True)
            total += len(content)
            if type(item['size_bytes']) is not int or item['size_bytes'] != len(content) or len(content) > limits.max_file_bytes or total > limits.max_artifact_bytes or hashlib.sha256(content).hexdigest() != item['sha256'] or any(file.name == name for file in files):
                raise ValueError()
            files.append(SandboxFile(name, content))
        return SandboxResult(data['success'], data['error_code'], data['exit_code'], data['duration_ms'],
            stdout.decode('utf-8', errors='replace'), stderr.decode('utf-8', errors='replace'), tuple(files))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise sandbox_error("SANDBOX_PROTOCOL_ERROR", 502) from None
