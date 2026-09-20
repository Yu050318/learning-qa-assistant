import os
import unittest
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

os.environ["LANGSMITH_TRACING"] = "false"

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import ChatTurn, RunContext, WebSearchBatch, WebSearchResult
from app.workflows.agent import AgentRAGWorkflow


class ScriptedModel(BaseChatModel):
    responses: list[Any]
    position: int = 0
    seen_messages: list = []
    seen_calls: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages = list(messages)
        self.seen_calls.append(list(messages))
        response = self.responses[self.position]
        self.position += 1
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


class Models:
    def __init__(self, model):
        self.model = model

    def agent_model(self, provider, timeout_seconds):
        return self.model, provider, "scripted"

    def agent_fallback(self, provider, timeout_seconds):
        return None


class FallbackModels(Models):
    def __init__(self, primary, fallback):
        super().__init__(primary)
        self.fallback = fallback

    def agent_fallback(self, provider, timeout_seconds):
        return self.fallback, "ollama", "fallback-scripted"

    def agent_model(self, provider, timeout_seconds):
        if provider == "ollama":
            return self.fallback, "ollama", "fallback-scripted"
        return super().agent_model(provider, timeout_seconds)


class ForbiddenRetriever:
    def retrieve(self, *args, **kwargs):
        raise AssertionError("web mode touched knowledge retrieval")


class Web:
    calls = 0

    def search(self, query, timeout_seconds):
        self.calls += 1
        self.query = query
        return WebSearchBatch([WebSearchResult(
            "官方资料", "https://example.com/docs", "公开内容", 0.9,
            datetime(2026, 9, 10, tzinfo=timezone.utc), None,
        )], 1)


class AgentV2Tests(unittest.TestCase):
    def test_context_budget_rejects_oversized_model_input(self):
        model = ScriptedModel(responses=[AIMessage(content='{"kind":"smalltalk","answer":"不应调用"}')])
        workflow = AgentRAGWorkflow(Settings(_env_file=None, agent_context_token_budget=4000), ForbiddenRetriever(), Models(model), Web())
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="问题",
            search_mode="auto", allowed_document_ids=(), selected_provider="deepseek",
            public_query=None, web_enabled=False,
        )

        with self.assertRaises(AppError) as raised:
            workflow.run(context, [ChatTurn("user", "长" * 15000)], "")

        self.assertEqual("CONTEXT_LIMIT_EXCEEDED", raised.exception.code)
        self.assertEqual(0, model.position)

    def test_retryable_model_failure_falls_back_without_repeating_web_search(self):
        primary = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "call-1", "type": "tool_call"}]),
            httpx.ConnectError("primary unavailable"),
        ])
        fallback = ScriptedModel(responses=[
            AIMessage(content='{"kind":"grounded","answer":"公开结论[1]"}'),
        ])
        web = Web()
        settings = Settings(_env_file=None, web_search_enabled=True, allow_model_fallback=True)
        workflow = AgentRAGWorkflow(settings, ForbiddenRetriever(), FallbackModels(primary, fallback), web)
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="事实问题",
            search_mode="web", allowed_document_ids=(), selected_provider="deepseek",
            public_query="公开查询", web_enabled=True,
        )

        result = workflow.run(context, [], "")

        self.assertEqual("ollama", result.answer.provider)
        self.assertTrue(result.run_metadata["fallback_used"])
        self.assertEqual(3, result.run_metadata["model_attempts"])
        self.assertEqual(1, web.calls)

    def test_non_retryable_model_failure_does_not_fallback(self):
        primary = ScriptedModel(responses=[ValueError("invalid request")])
        fallback = ScriptedModel(responses=[AIMessage(content='{"kind":"smalltalk","answer":"不应调用"}')])
        settings = Settings(_env_file=None, allow_model_fallback=True)
        workflow = AgentRAGWorkflow(settings, ForbiddenRetriever(), FallbackModels(primary, fallback), Web())
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="你好",
            search_mode="auto", allowed_document_ids=(), selected_provider="deepseek",
            public_query=None, web_enabled=False,
        )

        with self.assertRaises(ValueError):
            workflow.run(context, [], "")

        self.assertEqual(0, fallback.position)

    def test_repair_call_can_fallback_after_retryable_failure(self):
        primary = ScriptedModel(responses=[
            AIMessage(content="invalid-json"),
            httpx.ConnectError("primary unavailable during repair"),
        ])
        fallback = ScriptedModel(responses=[
            AIMessage(content='{"kind":"smalltalk","answer":"你好"}'),
        ])
        settings = Settings(_env_file=None, allow_model_fallback=True)
        workflow = AgentRAGWorkflow(settings, ForbiddenRetriever(), FallbackModels(primary, fallback), Web())
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="你好",
            search_mode="auto", allowed_document_ids=(), selected_provider="deepseek",
            public_query=None, web_enabled=False,
        )

        result = workflow.run(context, [], "")

        self.assertEqual("ollama", result.answer.provider)
        self.assertTrue(result.run_metadata["fallback_used"])
        self.assertTrue(result.run_metadata["repair_used"])
        self.assertEqual(3, result.run_metadata["model_attempts"])

    def test_citation_repair_receives_valid_source_numbers(self):
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "call-1", "type": "tool_call"}]),
            AIMessage(content='{"kind":"clarification","answer":"请补充"}'),
            AIMessage(content='{"kind":"grounded","answer":"公开结论[1]"}'),
        ])
        workflow = AgentRAGWorkflow(
            Settings(_env_file=None, web_search_enabled=True), ForbiddenRetriever(), Models(model), Web()
        )
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="事实问题",
            search_mode="web", allowed_document_ids=(), selected_provider="deepseek",
            public_query="公开查询", web_enabled=True,
        )
        workflow.run(context, [], "")
        self.assertIn("有效来源编号：[1]", model.seen_calls[-1][-1].content)

    def test_system_prompt_includes_current_search_mode(self):
        model = ScriptedModel(responses=[AIMessage(content='{"kind":"insufficient","answer":"资料不足"}')])
        workflow = AgentRAGWorkflow(Settings(_env_file=None), ForbiddenRetriever(), Models(model), Web())
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="事实问题",
            search_mode="knowledge", allowed_document_ids=(), selected_provider="deepseek",
            public_query=None, web_enabled=False,
        )
        workflow.run(context, [], "")
        self.assertIn("knowledge", model.seen_messages[0].content)
        self.assertIn("非空证据", model.seen_messages[0].content)

    def test_web_mode_uses_frozen_query_and_returns_registered_citation(self):
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "search_web", "args": {}, "id": "call-1", "type": "tool_call"}]),
            AIMessage(content='{"kind":"grounded","answer":"公开结论[1]"}'),
        ])
        web = Web()
        workflow = AgentRAGWorkflow(Settings(_env_file=None, web_search_enabled=True), ForbiddenRetriever(), Models(model), web)
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="私有上下文问题",
            search_mode="web", allowed_document_ids=(), selected_provider="deepseek",
            public_query="公开查询", web_enabled=True,
        )
        result = workflow.run(context, [], "")
        self.assertEqual("公开查询", web.query)
        self.assertEqual("web", result.citations[0]["type"])
        self.assertEqual(1, result.run_metadata["web_attempts"])
        self.assertEqual(1, result.run_metadata["tavily_credits"])
        self.assertIn("elapsed_ms", result.run_metadata)
        self.assertEqual(2, result.run_metadata["usage_by_model"]["deepseek/scripted"]["attempts"])

    def test_invalid_final_json_is_repaired_once(self):
        model = ScriptedModel(responses=[
            AIMessage(content="not-json"),
            AIMessage(content='{"kind":"smalltalk","answer":"你好"}'),
        ])
        workflow = AgentRAGWorkflow(Settings(_env_file=None), ForbiddenRetriever(), Models(model), Web())
        context = RunContext(
            request_id="request", user_id=uuid4(), session_id=uuid4(), question="你好",
            search_mode="auto", allowed_document_ids=(), selected_provider="deepseek",
            public_query=None, web_enabled=False,
        )
        result = workflow.run(context, [], "")
        self.assertEqual("你好", result.answer.content)
        self.assertTrue(result.run_metadata["repair_used"])
        self.assertEqual(2, result.run_metadata["model_attempts"])


if __name__ == "__main__":
    unittest.main()
