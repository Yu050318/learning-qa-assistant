"""Declarative public HTTPS JSON calls; shares the existing DNS-pinned transport."""
import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from threading import BoundedSemaphore
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from app.manager.run_checkpoint import CONTROL_ERRORS
from app.core.errors import AppError
from app.entity.bo.capabilities import HttpToolDeclaration
from app.manager.search.scrapling import ScraplingWebReader, pinned_public_session


def _no_remote_schema(uri):
    raise ValueError("External schema retrieval is disabled")


class HttpToolRuntime:
    def __init__(self, settings, *, resolve=None, request=None):
        self.settings = settings
        self.reader = ScraplingWebReader(settings, resolve=resolve)
        self.request = request or self._request
        self.dns = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool-dns")
        self.dns_slots = BoundedSemaphore(4)

    def close(self):
        self.dns.shutdown(wait=False, cancel_futures=True)

    def _addresses(self, url, remaining):
        if not self.dns_slots.acquire(blocking=False):
            raise AppError("HTTP_TOOL_DNS_BUSY", "工具域名解析繁忙", 503)
        try:
            future = self.dns.submit(self.reader._validate_url, url)
        except Exception:
            self.dns_slots.release()
            raise
        future.add_done_callback(lambda _: self.dns_slots.release())
        try:
            while True:
                try:
                    return future.result(timeout=min(.1, remaining()))
                except FutureTimeout:
                    remaining()
        finally:
            future.cancel()

    @staticmethod
    def _request(method, url, addresses, headers, body, timeout, receive):
        with pinned_public_session(url, addresses) as session:
            return session.request(method, url, headers=headers, data=body, timeout=timeout,
                allow_redirects=False, verify=True, content_callback=receive)

    def invoke(self, declaration: HttpToolDeclaration, arguments, *, deadline, check=lambda: None, approval=None, identity=None):
        # Revalidate even when a caller hands us a previously constructed mutable
        # declaration: nested dicts can otherwise bypass frozen-model checks.
        try:
            tool = HttpToolDeclaration.model_validate_json(declaration.model_dump_json())
        except ValueError:
            raise AppError("HTTP_TOOL_INVALID_CONFIGURATION", "工具配置未通过校验", 503) from None
        deadline = min(deadline, monotonic() + tool.timeout_seconds)

        def remaining():
            check()
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise AppError("AGENT_TIMEOUT", "工具调用超时", 504)
            return seconds

        remaining()
        if not tool.enabled or tool.requires_approval and not self.settings.tool_approvals_enabled:
            raise AppError("HTTP_TOOL_UNAVAILABLE", "工具已停用或尚需审批", 409)
        if tool.requires_approval:
            try:
                if approval is None:
                    raise ValueError("Missing approval")
                from app.entity.bo.tool_approvals import RunToolApproval
                raw = approval.model_dump_json(warnings=False) if isinstance(approval, RunToolApproval) else json.dumps(approval)
                grant = RunToolApproval.model_validate_json(raw)
                if not identity or not grant.allows(tool.name, **identity):
                    raise ValueError("Approval identity differs")
            except (ValueError, TypeError):
                raise AppError("HTTP_TOOL_APPROVAL_REQUIRED", "工具缺少绑定当前 Run 的明确批准", 403) from None
        try:
            raw = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > 65536:
                raise ValueError("arguments too large")
            arguments = json.loads(raw)
            Draft202012Validator(tool.input_schema, registry=Registry(retrieve=_no_remote_schema),
                format_checker=FormatChecker()).validate(arguments)
        except (ValidationError, ValueError, TypeError, RecursionError, UnicodeError, Unresolvable):
            raise AppError("HTTP_TOOL_INVALID_INPUT", "工具参数未通过结构或大小校验", 422) from None
        headers = {"Accept": "application/json", "User-Agent": "LearningQA-Tool/1.0", **tool.static_headers}
        secrets = []
        for name, reference in tool.secret_headers.items():
            value = os.environ.get(reference) if reference in self.settings.capability_secret_names else None
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value) or len(value) > 4096:
                raise AppError("HTTP_TOOL_NOT_CONFIGURED", "工具凭据尚未配置", 503)
            headers[name] = value
            secrets.append(value)
        url, body = tool.endpoint, None
        if tool.method == "GET":
            parsed = urlsplit(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            pairs.extend((key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)) for key, value in arguments.items())
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), ""))
        else:
            body = raw
            headers["Content-Type"] = "application/json"
        addresses = self._addresses(url, remaining)
        chunks, size, abort = [], 0, None

        def receive(chunk):
            nonlocal size, abort
            try:
                remaining()
                size += len(chunk)
                if size > tool.max_response_bytes:
                    raise AppError("HTTP_TOOL_RESPONSE_TOO_LARGE", "工具响应超过大小限制", 413)
            except AppError as error:
                abort = error
                return 0
            chunks.append(chunk)
            return len(chunk)

        attempted = False
        try:
            timeout = remaining()
            attempted = True
            response = self.request(tool.method, url, addresses, headers, body, timeout, receive)
            if abort:
                raise abort
            remaining()
            if 300 <= response.status_code < 400:
                raise AppError("HTTP_TOOL_REDIRECT_BLOCKED", "工具地址重定向被拒绝，请配置最终地址", 502)
            if not 200 <= response.status_code < 300:
                raise AppError("HTTP_TOOL_BAD_STATUS", "工具服务返回失败状态", 502)
            content_type = next((str(value).split(";", 1)[0].strip().lower() for key, value in response.headers.items() if key.lower() == "content-type"), "")
            if content_type != "application/json" and not (content_type.startswith("application/") and content_type.endswith("+json")):
                raise AppError("HTTP_TOOL_INVALID_RESPONSE", "工具响应不是 JSON", 502)
            raw_response = b"".join(chunks)
            data = json.loads(raw_response, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(data, dict):
                raise AppError("HTTP_TOOL_INVALID_RESPONSE", "工具响应必须为 JSON 对象", 502)
            canonical = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if any(value in canonical or json.dumps(value, ensure_ascii=True)[1:-1] in canonical for value in secrets):
                raise AppError("HTTP_TOOL_UNSAFE_RESPONSE", "工具响应包含凭据，已拒绝保存", 502)
            return {"status": "ok", "data": data}
        except AppError as error:
            if abort:
                error = abort
            if not tool.idempotent and attempted and error.code not in CONTROL_ERRORS:
                raise AppError("TOOL_OUTCOME_UNCERTAIN", "工具执行结果尚未确认，需人工核查", 409) from None
            raise error from None
        except Exception:
            if abort:
                raise abort from None
            if not tool.idempotent and attempted:
                raise AppError("TOOL_OUTCOME_UNCERTAIN", "工具执行结果尚未确认，需人工核查", 409) from None
            raise AppError("HTTP_TOOL_UNAVAILABLE", "工具调用失败", 502) from None
