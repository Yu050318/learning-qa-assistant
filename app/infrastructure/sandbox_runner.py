"""Standalone trusted entrypoint. Only runs generated code as PID 1 in a container."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time


class RunnerFailure(Exception):
    pass


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    return value


def read_request(stream):
    raw = stream.read(131073)
    if len(raw) > 131072:
        raise RunnerFailure("SANDBOX_INVALID_REQUEST")
    try:
        value = json.loads(raw)
        if set(value) != {"schema_version", "language", "source", "limits"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        source, limits = value["source"], value["limits"]
        if value["language"] not in {"python", "sh"} or not isinstance(source, str) or not source.strip() or "\x00" in source or len(source.encode("utf-8")) > 65536:
            raise ValueError()
        expected = {"timeout_seconds", "output_bytes", "max_files", "max_file_bytes", "max_artifact_bytes"}
        if set(limits) != expected:
            raise ValueError()
        timeout = limits["timeout_seconds"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
            raise ValueError()
        integer(limits["output_bytes"], 1024, 65536)
        integer(limits["max_files"], 1, 16)
        integer(limits["max_file_bytes"], 1024, 2097152)
        integer(limits["max_artifact_bytes"], limits["max_file_bytes"], 4194304)
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise RunnerFailure("SANDBOX_INVALID_REQUEST") from None


def terminate_descendants():
    # Never enumerate/kill a host process namespace, even if imported in a test.
    if os.getpid() != 1:
        raise RunnerFailure("SANDBOX_INVALID_CONTEXT")
    deadline = time.monotonic() + 1
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass
        except ChildProcessError:
            pass
        descendants = [int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() and int(p.name) > 1]
        if not descendants:
            return
        if time.monotonic() >= deadline:
            raise RunnerFailure("SANDBOX_PROCESS_LIMIT")
        for pid in descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(.01)


def execute(request):
    import resource
    limits = request["limits"]
    timeout, max_output = limits["timeout_seconds"], limits["output_bytes"]
    def child_limits():
        cpu = math.ceil(timeout) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits["max_file_bytes"], limits["max_file_bytes"]))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    argv = (["/usr/local/bin/python", "-I", "-B", "-c", request["source"]] if request["language"] == "python"
            else ["/bin/sh", "-eu", "-c", request["source"]])
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd="/workspace", env={"HOME": "/workspace", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        close_fds=True, start_new_session=True, preexec_fn=child_limits)
    stdout, stderr = bytearray(), bytearray()
    lock, overflow = threading.Lock(), threading.Event()
    def read(pipe, destination):
        try:
            while chunk := pipe.read(4096):
                with lock:
                    available = max(0, max_output - len(stdout) - len(stderr))
                    destination.extend(chunk[:available])
                    if len(chunk) > available:
                        overflow.set()
        finally:
            pipe.close()
    threads = [threading.Thread(target=read, args=(process.stdout, stdout), daemon=True),
               threading.Thread(target=read, args=(process.stderr, stderr), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    error = None
    try:
        while process.poll() is None:
            if overflow.is_set():
                error = "SANDBOX_OUTPUT_LIMIT"
                break
            if time.monotonic() >= deadline:
                error = "SANDBOX_TIMEOUT"
                break
            time.sleep(.01)
    finally:
        terminate_descendants()
        process.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            raise RunnerFailure("SANDBOX_OUTPUT_LIMIT")
    if overflow.is_set():
        error = "SANDBOX_OUTPUT_LIMIT"
    if process.returncode and error is None:
        error = "SANDBOX_EXECUTION_FAILED"
    return process.returncode, bytes(stdout), bytes(stderr), error


def collect_files(limits):
    files, visited, total = [], 0, 0
    def walk(fd, prefix, depth):
        nonlocal visited, total
        if depth > 4:
            raise RunnerFailure("SANDBOX_FILE_LIMIT")
        for name in sorted(os.listdir(fd)):
            visited += 1
            if visited > 128:
                raise RunnerFailure("SANDBOX_FILE_LIMIT")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                raise RunnerFailure("SANDBOX_UNSAFE_FILE")
            path = prefix + name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    walk(child, path + '/', depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if len(files) >= limits["max_files"]:
                    raise RunnerFailure("SANDBOX_FILE_LIMIT")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    actual = os.fstat(file_fd)
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise RunnerFailure("SANDBOX_UNSAFE_FILE")
                    with os.fdopen(file_fd, 'rb', closefd=False) as source:
                        content = source.read(limits["max_file_bytes"] + 1)
                    total += len(content)
                    if len(content) > limits["max_file_bytes"] or total > limits["max_artifact_bytes"]:
                        raise RunnerFailure("SANDBOX_FILE_LIMIT")
                    files.append({"name": path, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                        "content_b64": base64.b64encode(content).decode('ascii')})
                finally:
                    os.close(file_fd)
            else:
                raise RunnerFailure("SANDBOX_UNSAFE_FILE")
    root = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(root, '', 0)
    finally:
        os.close(root)
    return files


def main():
    started = time.monotonic()
    result = {"schema_version": 1, "success": False, "error_code": None, "exit_code": None,
              "stdout_b64": "", "stderr_b64": "", "duration_ms": 0, "files": []}
    try:
        if sys.platform != 'linux' or os.getpid() != 1 or os.getuid() != 65532 or Path.cwd() != Path('/workspace'):
            raise RunnerFailure("SANDBOX_INVALID_CONTEXT")
        request = read_request(sys.stdin.buffer)
        exit_code, stdout, stderr, error = execute(request)
        result.update(exit_code=exit_code, stdout_b64=base64.b64encode(stdout).decode('ascii'),
            stderr_b64=base64.b64encode(stderr).decode('ascii'), error_code=error)
        if error is None:
            result['files'] = collect_files(request['limits'])
            result['success'] = True
    except RunnerFailure as error:
        result['error_code'] = str(error)
    except Exception:
        result['error_code'] = 'SANDBOX_EXECUTION_FAILED'
    result['duration_ms'] = round((time.monotonic() - started) * 1000)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
