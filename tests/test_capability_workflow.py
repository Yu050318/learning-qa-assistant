from dataclasses import replace
from types import SimpleNamespace
from time import monotonic
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.api.schemas import V2ChatResponse
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, ChatTurn, RunContext
from app.infrastructure.persistence.models import ChatRun, ChatRunOperation, utcnow
from app.infrastructure.http_tools import HttpToolRuntime
from app.workflows.supervisor import SupervisorWorkflow
from test_agent_v2 import Models, ScriptedModel, call, final, report
from test_capability_control import control, http_tool, save, skill
from test_chat_runs import system


@pytest.fixture
def workflow(control):
    s = control
    save(s, http_tool(input_schema={"type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False}))
    save(s, skill())
    execution = s.capabilities.freeze(s.uid, skill_name="research", public_query="public weather")
    requests = []
    def request(method, url, addresses, headers, body, timeout, receive):
        requests.append({"url": url, "body": body, "headers": headers})
        receive(b'{"answer":42}')
        return SimpleNamespace(status_code=200, headers={"Content-Type": "application/json"})
    client = HttpToolRuntime(s.settings, resolve=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))], request=request)
    model = ScriptedModel(responses=[call("search_tools", query="lookup"), call("invoke_public_tool", tool_name="public.lookup"),
        call("call_http_tool", query="public weather"), report("The answer is 42[1]"), final("42[1]")])
    value = SupervisorWorkflow(s.settings, None, Models(model), None, capabilities=s.capabilities, http_tools=client)
    context = RunContext(request_id="capability-workflow", user_id=s.uid, session_id=s.sid, question="public weather",
        search_mode="auto", allowed_document_ids=(), selected_provider="deepseek", public_query="public weather", web_enabled=False,
        capability_snapshot=execution.model_dump(mode="json"), deadline_monotonic=monotonic() + 30)
    try:
        yield SimpleNamespace(system=s, workflow=value, context=context, model=model, requests=requests)
    finally:
        client.close()


def test_custom_http_tool_reaches_real_evidence_path_without_private_context(workflow):
    s = workflow
    result = s.workflow.run(s.context, [ChatTurn("user", "PRIVATE_HISTORY_TOKEN")], "PRIVATE_SUMMARY_TOKEN")
    assert result.answer.content == "42[1]"
    assert result.citations[0]["type"] == "tool"
    assert result.citations[0]["tool_name"] == "public.lookup" and result.citations[0]["tool_version"] == 1
    assert result.run_metadata["registered_sources"] == {"tool": 1}
    assert result.run_metadata["tool_requests"] == 2  # discovery + HTTP invocation
    assert s.requests[0]["url"].endswith("?query=public+weather")
    assert "PRIVATE_" not in str(s.requests)
    public_calls = [messages for messages in s.model.seen_calls if "公共 HTTP 工具" in str(messages[0].content)]
    assert len(public_calls) == 2
    assert all("PRIVATE_" not in str(messages) for messages in public_calls)
    assert "delegate_sql" not in s.model.bound_tools[0] and "delegate_rag" not in s.model.bound_tools[0]
    assert s.model.bound_tools[2] == ["call_http_tool"]
    # The public response contract accepts the actual registered tool citation.
    from uuid import uuid4
    response = V2ChatResponse(message_id=uuid4(), answer=result.answer.content, model_provider="deepseek", model_name="fixture",
        search_mode="auto", citations=result.citations, token_usage=None, run_metadata=result.run_metadata)
    assert response.citations[0].tool_name == "public.lookup"


def test_supervisor_cannot_pass_private_task_as_http_arguments(workflow):
    s = workflow
    s.model.responses = [call("invoke_public_tool", tool_name="public.lookup", query="PRIVATE_DATA"),
        call("call_http_tool", query="public weather"), report("42[1]"), final("42[1]")]
    s.workflow.run(s.context, [], "")
    assert s.requests and "PRIVATE_DATA" not in str(s.requests)
    assert all("PRIVATE_DATA" not in str(messages) for messages in s.model.seen_calls if "公共 HTTP 工具" in str(messages[0].content))


def test_custom_tool_cannot_claim_prior_evidence_without_an_actual_call(workflow):
    s = workflow
    s.model.responses = [call("invoke_public_tool", tool_name="public.lookup"), report("I guessed 42", status="ok"),
        final("No verified result", kind="insufficient")]
    result = s.workflow.run(s.context, [], "")
    assert result.kind == "insufficient" and not result.citations and not s.requests
    assert result.run_metadata["failures"][0]["code"] == "INVALID_AGENT_OUTPUT"


def test_web_mode_accepts_actual_public_tool_citations(workflow):
    s = workflow
    frozen = s.system.capabilities.freeze(s.system.uid, skill_name="research", search_mode="web", public_query="public weather")
    context = replace(s.context, search_mode="web", capability_snapshot=frozen.model_dump(mode="json"))
    result = s.workflow.run(context, [], "")
    assert result.citations[0]["type"] == "tool"


def expire_lease(s, run_id):
    with s.database.sessions() as session:
        session.get(ChatRun, run_id).lease_expires_at = utcnow() - timedelta(seconds=2)
        session.commit()


def test_completed_http_receipt_is_reused_after_worker_takeover(workflow):
    w, s = workflow, workflow.system
    s.chat.agent_workflow = w.workflow
    w.model.responses = [call("invoke_public_tool", tool_name="public.lookup"), call("call_http_tool", query="public weather"),
        report("42[1]"), AppError("RUN_LEASE_LOST", "fixture lost owner before next model result", 409), final("42[1]")]
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "takeover-tool", uuid4())
    with patch("app.infrastructure.llms.providers.ModelRouter", return_value=Models(w.model)):
        with pytest.raises(AppError) as error:
            s.chat.execute_prepared(s.store, s.store.claim_next())
        assert error.value.code == "RUN_LEASE_LOST"
        assert len(w.requests) == 1
        expire_lease(s, run.id)
        message = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert len(w.requests) == 1 and message.content == "42[1]"
    assert message.citations[0]["tool_name"] == "public.lookup"
    assert s.store.read(s.uid, run.id)[0].state == "succeeded"


def test_nonidempotent_http_outcome_is_not_repeated_on_takeover(workflow):
    w, s = workflow, workflow.system
    save(s, http_tool(method="POST", idempotent=False, input_schema={"type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False}), create=False)
    s.chat.agent_workflow = w.workflow
    w.model.responses = [call("invoke_public_tool", tool_name="public.lookup"), call("call_http_tool", query="public weather")]
    attempts = []
    def uncertain(*args):
        attempts.append(True)
        raise OSError("connection lost after a possible write")
    w.workflow.http_tools.request = uncertain
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "uncertain-tool", uuid4())
    with patch("app.infrastructure.llms.providers.ModelRouter", return_value=Models(w.model)):
        for attempt in range(2):
            if attempt:
                expire_lease(s, run.id)
            with pytest.raises(AppError) as error:
                s.chat.execute_prepared(s.store, s.store.claim_next())
            assert error.value.code == "TOOL_OUTCOME_UNCERTAIN"
    assert attempts == [True]
    from sqlalchemy import select
    with s.database.sessions() as session:
        receipts = session.scalars(select(ChatRunOperation).where(ChatRunOperation.run_id == run.id, ChatRunOperation.readonly.is_(False))).all()
        assert len(receipts) == 1 and receipts[0].state == "uncertain" and receipts[0].attempts == 1


def test_hitl_public_tool_uses_only_explicit_public_query_in_same_run(workflow):
    w, s = workflow, workflow.system
    s.chat.agent_workflow = w.workflow
    w.model.responses = [final("Which city?", kind="clarification"),
        call("invoke_public_tool", tool_name="public.lookup"), call("call_http_tool", query="public weather Beijing"),
        report("42[1]"), final("42[1]")]
    run = s.runs.start(s.uid, s.sid, AgentChatInput("public weather", skill_name="research"), "hitl-http", uuid4())
    with patch("app.infrastructure.llms.providers.ModelRouter", return_value=Models(w.model)):
        first = s.chat.execute_prepared(s.store, s.store.claim_next())
        paused, _ = s.store.read(s.uid, run.id)
        assert paused.state == "waiting"
        s.store.resume(s.uid, run.id, paused.waiting["checkpoint_revision"], paused.resume_key,
            "PRIVATE_SUPPLEMENT_TOKEN", public_query="public weather Beijing")
        result = s.chat.execute_prepared(s.store, s.store.claim_next())
    assert result.id == first.id == run.message_id and result.content == "42[1]"
    assert len(w.requests) == 1 and w.requests[0]["url"].endswith("?query=public+weather+Beijing")
    assert "PRIVATE_" not in str(w.requests)
    public_calls = [messages for messages in w.model.seen_calls if "公共 HTTP 工具" in str(messages[0].content)]
    assert public_calls and all("PRIVATE_" not in str(messages) for messages in public_calls)
    assert run.execution_snapshot["context"]["public_query"] == "public weather"
