import json
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from curl_cffi import CurlOpt

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.capabilities import HttpToolDeclaration
from app.infrastructure.http_tools import HttpToolRuntime


@pytest.fixture
def runtime():
    settings = Settings(_env_file=None, capability_secret_names=["PUBLIC_TOOL_TOKEN"])
    calls = []

    def request(method, url, addresses, headers, body, timeout, receive):
        calls.append({"method": method, "url": url, "addresses": addresses, "headers": headers, "body": body, "timeout": timeout})
        receive(b'{"answer":42}')
        return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"})

    client = HttpToolRuntime(settings, resolve=lambda *a, **kw: [(None, None, None, None, ("93.184.216.34", 443))], request=request)
    try:
        yield SimpleNamespace(client=client, calls=calls, settings=settings)
    finally:
        client.close()


def declaration(**updates):
    return HttpToolDeclaration(**{"name": "public.lookup", "description": "Lookup", "group": "public",
        "endpoint": "https://api.service.com/lookup?fixed=1",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 500}}, "required": ["query"], "additionalProperties": False}, **updates})


def invoke(runtime, tool=None, arguments=None, **kwargs):
    return runtime.client.invoke(tool or declaration(), {"query": "hello"} if arguments is None else arguments,
        deadline=monotonic() + 5, **kwargs)


def fails(code, function, *args, **kwargs):
    with pytest.raises(AppError) as error:
        function(*args, **kwargs)
    assert error.value.code == code


def test_get_and_post_validate_arguments_and_keep_original_query(runtime):
    assert invoke(runtime) == {"status": "ok", "data": {"answer": 42}}
    assert runtime.calls[0]["url"].endswith("?fixed=1&query=hello")
    assert runtime.calls[0]["body"] is None
    invoke(runtime, declaration(method="POST"))
    assert json.loads(runtime.calls[1]["body"]) == {"query": "hello"}
    assert runtime.calls[1]["headers"]["Content-Type"] == "application/json"
    assert 0 < runtime.calls[0]["timeout"] <= 5


@pytest.mark.parametrize("arguments", [{}, {"query": 123}, {"query": "x", "unexpected": True}, {"query": "x" * 501}])
def test_invalid_arguments_never_call_network(runtime, arguments):
    fails("HTTP_TOOL_INVALID_INPUT", invoke, runtime, arguments=arguments)
    assert not runtime.calls


def test_missing_reference_and_mutated_declaration_fail_closed(runtime):
    tool = declaration(input_schema={"type": "object", "properties": {"query": {"$ref": "#/$defs/absent"}}})
    fails("HTTP_TOOL_INVALID_INPUT", invoke, runtime, tool)
    tool.static_headers["Host"] = "localhost"
    fails("HTTP_TOOL_INVALID_CONFIGURATION", invoke, runtime, tool)
    assert not runtime.calls


def test_missing_secret_and_pending_approval_do_not_call_network(runtime, monkeypatch):
    monkeypatch.delenv("PUBLIC_TOOL_TOKEN", raising=False)
    fails("HTTP_TOOL_NOT_CONFIGURED", invoke, runtime, declaration(secret_headers={"Authorization": "PUBLIC_TOOL_TOKEN"}))
    fails("HTTP_TOOL_UNAVAILABLE", invoke, runtime, declaration(requires_approval=True))
    fails("HTTP_TOOL_UNAVAILABLE", invoke, runtime, declaration(enabled=False))
    monkeypatch.setenv("UNRELATED_SECRET", "private")
    fails("HTTP_TOOL_NOT_CONFIGURED", invoke, runtime, declaration(secret_headers={"Authorization": "UNRELATED_SECRET"}))
    assert not runtime.calls


def test_private_dns_answer_is_rejected(runtime):
    runtime.client.reader.resolve = lambda *a, **kw: [(None, None, None, None, ("127.0.0.1", 443))]
    fails("UNSAFE_WEB_URL", invoke, runtime)
    assert not runtime.calls


@pytest.mark.parametrize("status,content_type,body,code", [
    (302, "application/json", b"{}", "HTTP_TOOL_REDIRECT_BLOCKED"),
    (503, "application/json", b"{}", "HTTP_TOOL_BAD_STATUS"),
    (200, "text/html", b"{}", "HTTP_TOOL_INVALID_RESPONSE"),
    (200, "application/json", b"[]", "HTTP_TOOL_INVALID_RESPONSE"),
    (200, "application/json", b'{"x":NaN}', "HTTP_TOOL_UNAVAILABLE"),
])
def test_unusable_responses_return_safe_codes(runtime, status, content_type, body, code):
    def request(*args):
        args[-1](body)
        return SimpleNamespace(status_code=status, headers={"Content-Type": content_type})
    runtime.client.request = request
    fails(code, invoke, runtime)


def test_body_size_and_cancellation_abort_download(runtime):
    def large(*args):
        assert args[-1](b"x" * 1025) == 0
        raise RuntimeError("curl aborted")
    runtime.client.request = large
    fails("HTTP_TOOL_RESPONSE_TOO_LARGE", invoke, runtime, declaration(max_response_bytes=1024))

    cancelled = Event()
    def check():
        if cancelled.is_set():
            raise AppError("RUN_CANCEL_REQUESTED", "Cancelled", 409)
    def cancel(*args):
        cancelled.set()
        assert args[-1](b"{}") == 0
        raise RuntimeError("curl aborted")
    runtime.client.request = cancel
    fails("RUN_CANCEL_REQUESTED", invoke, runtime, declaration(idempotent=False), check=check)


def test_slow_dns_is_bounded_and_does_not_create_unbounded_pending_work(runtime):
    release = Event()
    runtime.client.reader.resolve = lambda *a, **kw: (release.wait(3) or [])
    try:
        fails("AGENT_TIMEOUT", runtime.client.invoke, declaration(), {"query": "hi"}, deadline=monotonic() + .05)
        assert not runtime.calls
    finally:
        release.set()


def test_nonidempotent_transport_failure_is_uncertain(runtime):
    def failed(*args):
        raise OSError("potential side effect before lost response")
    runtime.client.request = failed
    fails("TOOL_OUTCOME_UNCERTAIN", invoke, runtime, declaration(method="POST", idempotent=False))
    fails("HTTP_TOOL_UNAVAILABLE", invoke, runtime)


def test_credential_echo_is_not_persisted(runtime, monkeypatch):
    monkeypatch.setenv("PUBLIC_TOOL_TOKEN", "fixture-never-persist")
    def echo(method, url, addresses, headers, body, timeout, receive):
        receive(json.dumps({"echo": headers["Authorization"]}).encode())
        return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"})
    runtime.client.request = echo
    fails("HTTP_TOOL_UNSAFE_RESPONSE", invoke, runtime, declaration(secret_headers={"Authorization": "PUBLIC_TOOL_TOKEN"}))


def test_real_transport_disables_redirects_and_proxies_and_pins_validated_ip(runtime):
    session = MagicMock()
    def request(method, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
        kwargs["content_callback"](b'{"ok":true}')
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"})
    session.request.side_effect = request
    runtime.client.request = runtime.client._request
    with patch("app.infrastructure.search.scrapling.Session") as factory:
        factory.return_value.__enter__.return_value = session
        assert invoke(runtime)["data"] == {"ok": True}
        assert factory.call_args.kwargs["trust_env"] is False
        assert factory.call_args.kwargs["curl_options"][CurlOpt.RESOLVE] == [b"api.service.com:443:93.184.216.34"]
