import os
import unittest
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

os.environ["LANGSMITH_TRACING"] = "false"

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk, ChatTurn, RunContext, SearchHit, WebSearchBatch, WebSearchResult
from app.workflows.supervisor import SupervisorWorkflow
from app.workflows.agent import AnswerStreamParser


class ScriptedModel(BaseChatModel):
    responses: list[Any]
    position: int = 0
    seen_messages: list = []
    seen_calls: list = []
    bound_tools: list = []

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append([tool.name for tool in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages = list(messages)
        self.seen_calls.append(list(messages))
        response = self.responses[self.position]
        self.position += 1
        if isinstance(response, Exception):
            raise response
        return ChatResult(generations=[ChatGeneration(message=response)])


class StreamingScriptedModel(ScriptedModel):
    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages = list(messages)
        self.seen_calls.append(list(messages))
        response = self.responses[self.position]
        self.position += 1
        for part in response:
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))


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


class Retriever:
    def __init__(self, score: float):
        self.score = score
        self.calls = 0

    def retrieve(self, user_id, query, document_ids):
        self.calls += 1
        return [SearchHit(Chunk(
            chunk_id="chunk-1", user_id=str(user_id), document_id=str(document_ids[0]),
            chunk_index=0, content="知识库内容", source_name="资料.pdf",
        ), self.score)]


class Web:
    calls = 0

    def search(self, query, timeout_seconds):
        self.calls += 1
        self.query = query
        return WebSearchBatch([WebSearchResult(
            "官方资料", "https://example.com/docs", "公开内容", 0.9,
            datetime(2026, 9, 10, tzinfo=timezone.utc), None,
        )], 1)


class WebReader:
    calls = 0

    def read(self, url, timeout_seconds):
        self.calls += 1
        self.url = url
        return WebSearchResult(
            "完整页面", url, "Scrapling 提取的完整正文", None,
            datetime(2026, 9, 10, tzinfo=timezone.utc), None,
        )


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}])


def report(summary="已取得证据[1]", status="ok"):
    import json
    return AIMessage(content=json.dumps({"status": status, "summary": summary}, ensure_ascii=False))


def final(answer="结论[1]", kind="grounded"):
    import json
    return AIMessage(content=json.dumps({"kind": kind, "answer": answer}, ensure_ascii=False))


class SQL:
    def schema(self, remaining):
        return {"source_name": "业务库", "tables": {"sales.orders": [{"name": "amount", "type": "INTEGER"}]}}

    def query(self, query, remaining):
        return {"source_name": "业务库", "query": query, "columns": ["total"], "rows": [[42]], "truncated": False}


class AgentV2Tests(unittest.TestCase):
    def context(self, mode="auto", **kwargs):
        values = dict(request_id="request", user_id=uuid4(), session_id=uuid4(), question="事实问题",
                      search_mode=mode, allowed_document_ids=(), selected_provider="deepseek",
                      public_query="公开查询", web_enabled=True)
        values.update(kwargs)
        return RunContext(**values)

    def workflow(self, responses, **kwargs):
        model = ScriptedModel(responses=responses)
        self.model = model
        self.web = Web()
        self.web.calls = 0
        return SupervisorWorkflow(Settings(_env_file=None), kwargs.pop("retriever", ForbiddenRetriever()),
                                  Models(model), self.web, **kwargs)

    def test_supervisor_delegates_web_with_frozen_query_and_registered_citation(self):
        workflow = self.workflow([call("delegate_web", task="联网核实"), call("search_web"), report(), final()])
        result = workflow.run(self.context("web"), [], "")
        self.assertEqual("公开查询", self.web.query)
        self.assertEqual("web", result.citations[0]["type"])
        self.assertEqual(4, result.run_metadata["model_attempts"])
        self.assertEqual({"web": 1}, result.run_metadata["delegations"])
        self.assertEqual(1, result.run_metadata["tavily_credits"])

    def test_rag_delegation_retains_user_document_scope(self):
        retriever = Retriever(.02)
        workflow = self.workflow([call("delegate_rag", task="查询资料"), call("search_knowledge", query="资料"), report(), final()], retriever=retriever)
        result = workflow.run(self.context("knowledge", allowed_document_ids=(uuid4(),)), [], "")
        self.assertEqual(1, retriever.calls)
        self.assertEqual("knowledge", result.citations[0]["type"])
        self.assertEqual(0, self.web.calls)

    def test_sql_and_web_share_numbers_and_supervisor_combines_results(self):
        workflow = self.workflow([
            call("delegate_sql", task="统计销量"), call("describe_sql_schema"),
            call("execute_sql", query="SELECT SUM(amount) FROM sales.orders"), report(),
            call("delegate_web", task="查官网公告"), call("search_web"), report("公告[2]"),
            final("销量 42[1]；公告[2]"),
        ], sql=SQL())
        result = workflow.run(self.context(), [], "")
        self.assertEqual(["sql", "web"], [source["type"] for source in result.citations])
        self.assertEqual([1, 2], [source["number"] for source in result.citations])
        self.assertEqual(["sql", "web"], [event["agent"] for event in result.run_metadata["agent_events"]])

    def test_specified_url_works_without_tavily(self):
        reader = WebReader()
        workflow = self.workflow([call("delegate_web", task="读取网页"),
                                  call("read_web_page", url="https://example.com/docs"), report(), final()], web_reader=reader)
        result = workflow.run(self.context("web", web_enabled=False, public_query=None,
                                          web_urls=("https://example.com/docs",)), [], "")
        self.assertEqual(0, self.web.calls)
        self.assertEqual("https://example.com/docs", reader.url)
        self.assertIn("完整正文", result.citations[0]["excerpt"])

    def test_reading_search_result_replaces_snippet_with_page_evidence(self):
        workflow = self.workflow([call("delegate_web", task="搜索并核实原文"), call("search_web"),
                                  call("read_web_page", url="https://example.com/docs"), report(), final()], web_reader=WebReader())
        result = workflow.run(self.context("web"), [], "")
        self.assertEqual(1, result.citations[0]["number"])
        self.assertIn("完整正文", result.citations[0]["excerpt"])
        self.assertEqual(1, result.run_metadata["web_reads"])

    def test_webpage_cannot_authorize_unrequested_urls(self):
        reader = WebReader()
        reader.calls = 0
        workflow = self.workflow([call("delegate_web", task="读取网页"),
                                  call("read_web_page", url="https://other.example/private"), report("读取失败", "empty"),
                                  final("未能读取指定页面", "insufficient")], web_reader=reader)
        result = workflow.run(self.context("web", web_urls=("https://example.com/docs",)), [], "")
        self.assertEqual(0, reader.calls)
        self.assertEqual("TOOL_NOT_ALLOWED", result.run_metadata["failures"][0]["code"])
        self.assertEqual("partial", result.run_metadata["agent_events"][0]["status"])

    def test_stream_only_supervisor_answer_and_emits_agent_events(self):
        workflow = self.workflow([call("delegate_web", task="查询"), call("search_web"), report("内部子任务结论[1]"), final("最终结论[1]")])
        events = []
        result = workflow.run(self.context("web"), [], "", lambda event, value: events.append((event, value)))
        self.assertEqual("最终结论[1]", result.answer.content)
        text = "".join(value for event, value in events if event == "answer_delta")
        self.assertEqual("最终结论[1]", text)
        self.assertTrue(any(event == "agent_started" for event, _ in events))
        self.assertTrue(any(event == "agent_finished" for event, _ in events))

    def test_smalltalk_can_skip_specialists(self):
        workflow = self.workflow([final("你好", "smalltalk")])
        result = workflow.run(self.context(web_enabled=False), [], "")
        self.assertEqual({}, result.run_metadata["delegations"])
        self.assertEqual("你好", result.answer.content)

    def test_ambiguous_request_can_ask_for_clarification(self):
        workflow = self.workflow([final("你希望查询哪个时间段的销量？", "clarification")])
        result = workflow.run(self.context("sql"), [], "")
        self.assertEqual("clarification", result.kind)

    def test_invalid_json_and_unknown_citations_repaired_once(self):
        workflow = self.workflow([call("delegate_web", task="查询"), call("search_web"), report(),
                                  final("伪造引用[99]"), final()])
        result = workflow.run(self.context("web"), [], "")
        self.assertTrue(result.run_metadata["repair_used"])
        self.assertIn("有效来源编号：[1]", self.model.seen_calls[-1][-1].content)

    def test_invalid_final_json_repaired_once(self):
        workflow = self.workflow([AIMessage(content="not-json"), final("你好", "smalltalk")])
        self.assertTrue(workflow.run(self.context(), [], "").run_metadata["repair_used"])

    def test_second_invalid_citation_is_rejected(self):
        workflow = self.workflow([final("伪造[99]"), final("伪造[98]")])
        with self.assertRaises(AppError):
            workflow.run(self.context(), [], "")

    def test_context_budget_rejects_oversized_model_input(self):
        workflow = self.workflow([final("不应调用", "smalltalk")])
        workflow.settings = Settings(_env_file=None, agent_context_token_budget=4000)
        with self.assertRaises(AppError) as raised:
            workflow.run(self.context(), [ChatTurn("user", "长" * 15000)], "")
        self.assertEqual("CONTEXT_LIMIT_EXCEEDED", raised.exception.code)
        self.assertEqual(0, self.model.position)

    def test_all_agents_share_model_budget(self):
        workflow = self.workflow([call("delegate_web", task="查询"), call("search_web"), report(), final()])
        workflow.settings = Settings(_env_file=None, agent_max_model_calls=2)
        with self.assertRaises(AppError) as raised:
            workflow.run(self.context("web"), [], "")
        self.assertEqual("AGENT_LIMIT_EXCEEDED", raised.exception.code)
        self.assertEqual(2, self.model.position)

    def test_delegation_budget(self):
        workflow = self.workflow([call("delegate_web", task="查询"), call("search_web"), report(),
                                  call("delegate_web", task="再次查询")])
        workflow.settings = Settings(_env_file=None, supervisor_max_per_agent=1)
        with self.assertRaises(AppError) as raised:
            workflow.run(self.context("web"), [], "")
        self.assertEqual("AGENT_LIMIT_EXCEEDED", raised.exception.code)

    def test_deadline_checked_before_model(self):
        workflow = self.workflow([])
        with self.assertRaises(AppError) as raised:
            workflow.run(self.context(deadline_monotonic=0), [], "")
        self.assertEqual("AGENT_TIMEOUT", raised.exception.code)

    def test_retryable_failure_falls_back_without_repeating_search(self):
        primary = ScriptedModel(responses=[call("delegate_web", task="查询"), call("search_web"), report(), httpx.ConnectError("unavailable")])
        fallback = ScriptedModel(responses=[final()])
        web = Web(); web.calls = 0
        workflow = SupervisorWorkflow(Settings(_env_file=None), ForbiddenRetriever(), FallbackModels(primary, fallback), web)
        result = workflow.run(self.context("web"), [], "")
        self.assertEqual("ollama", result.answer.provider)
        self.assertEqual(1, web.calls)
        self.assertTrue(result.run_metadata["fallback_used"])

    def test_non_retryable_model_error_is_not_retried(self):
        workflow = self.workflow([ValueError("invalid")])
        with self.assertRaises(ValueError):
            workflow.run(self.context(), [], "")
        self.assertEqual(1, self.model.position)

    def test_missing_sql_configuration_remains_failure(self):
        workflow = self.workflow([call("delegate_sql", task="查询销量"), call("describe_sql_schema"),
                                  report("数据库不可用", "empty"), final("业务数据库未配置，无法查询", "insufficient")])
        result = workflow.run(self.context("sql"), [], "")
        self.assertEqual("SQL_UNAVAILABLE", result.run_metadata["failures"][0]["code"])
        self.assertEqual([], result.citations)

    def test_fixed_modes_expose_only_authorized_delegate(self):
        for mode, tool in (("knowledge", "delegate_rag"), ("web", "delegate_web"), ("sql", "delegate_sql")):
            workflow = self.workflow([final("请明确范围", "clarification")])
            workflow.run(self.context(mode), [], "")
            self.assertEqual([tool], self.model.bound_tools[0])

    def test_requests_do_not_share_evidence_numbers_or_web_cache(self):
        sequence = [call("delegate_web", task="查询"), call("search_web"), report(), final()]
        workflow = self.workflow(sequence + sequence)
        first = workflow.run(self.context("web"), [], "")
        second = workflow.run(self.context("web"), [], "")
        self.assertEqual([1], [source["number"] for source in first.citations])
        self.assertEqual([1], [source["number"] for source in second.citations])
        self.assertEqual(2, self.web.calls)
        self.assertEqual(4, second.run_metadata["model_attempts"])

    def test_explicit_memory_write_is_preserved(self):
        from types import SimpleNamespace
        writes = []

        class Memories:
            def search(self, user_id, question):
                return []

            def create(self, user_id, content, **kwargs):
                writes.append((user_id, content))
                return SimpleNamespace(id=uuid4())

        workflow = self.workflow([call("save_memory", content="用户喜欢简洁回答"), final("已记住", "smalltalk")], memories=Memories())
        context = self.context(question="请记住，我喜欢简洁回答")
        workflow.run(context, [], "")
        self.assertEqual([(context.user_id, "用户喜欢简洁回答")], writes)

    def test_stream_parser_handles_partial_unicode(self):
        parser = AnswerStreamParser()
        output = []
        for part in ['{"kind":"smalltalk","ans', 'wer":"你\\u', '597d\\n世', '界！"}']:
            output.append(parser.feed(part)[1])
        self.assertEqual("你好\n世界！", "".join(output))


if __name__ == "__main__":
    unittest.main()
