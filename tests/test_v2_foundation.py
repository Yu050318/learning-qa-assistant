import unittest
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.schemas import V2ChatRequest, V2ChatResponse
from app.application.chat import prepare_public_query
from app.application.evidence import EvidenceRegistry
from app.application.sessions import SessionService
from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput, Chunk, SearchHit, WebSearchBatch, WebSearchResult
from app.infrastructure.persistence.models import Base, ChatSession, User
from app.infrastructure.persistence.repository import Repository
from app.infrastructure.search.tavily import TavilySearch
from app.infrastructure.vectorstores.milvus import MilvusVectorStore


class V2FoundationTests(unittest.TestCase):
    def test_session_locks_are_exact_and_reject_only_the_same_session(self):
        service = SessionService(MagicMock())
        first = UUID(int=1)
        colliding_under_the_old_striped_lock = UUID(int=65)
        with service.exclusive(first):
            with service.exclusive(colliding_under_the_old_striped_lock):
                with self.assertRaises(AppError) as raised:
                    with service.exclusive(first):
                        pass
        self.assertEqual("RESOURCE_BUSY", raised.exception.code)

    def test_milvus_connection_uses_configured_uri(self):
        settings = Settings(_env_file=None, milvus_uri="http://127.0.0.1:19531")
        client = MagicMock()
        with patch("app.infrastructure.vectorstores.milvus.MilvusClient", return_value=client) as constructor:
            with MilvusVectorStore(settings).connection():
                pass
        self.assertEqual("http://127.0.0.1:19531", constructor.call_args.kwargs["uri"])
        client.close.assert_called_once()

    def test_public_web_query_blocks_obvious_connection_string(self):
        payload = AgentChatInput(
            question="请联网查询",
            search_mode="web",
            web_query="postgresql://user:password@example.com/database",
        )
        with self.assertRaises(AppError) as raised:
            prepare_public_query(payload, True)
        self.assertEqual("WEB_QUERY_REQUIRED", raised.exception.code)

    def test_auto_mode_disables_blocked_public_query_with_warning(self):
        payload = AgentChatInput(
            question="请检查 api_key=abcdefghijklmnop 是否泄露",
            search_mode="auto",
        )
        query, enabled, warnings = prepare_public_query(payload, True)
        self.assertIsNone(query)
        self.assertFalse(enabled)
        self.assertEqual(("WEB_QUERY_BLOCKED",), warnings)

    def test_evidence_registry_numbers_and_validates_mixed_sources(self):
        registry = EvidenceRegistry(token_budget=500)
        document_id = uuid4()
        chunk_id = uuid4()
        result = registry.add_knowledge([
            SearchHit(Chunk(str(chunk_id), str(uuid4()), str(document_id), 0, "内部资料", "notes.md"), 0.8)
        ])
        self.assertEqual([1], [source["number"] for source in result["sources"]])
        registry.add_web(WebSearchBatch(results=[WebSearchResult(
            title="Official docs", url="https://example.com/docs#intro", content="公开资料", score=0.9,
            retrieved_at=datetime(2026, 9, 10, tzinfo=timezone.utc), published_at=None,
        )], credits=1))
        citations = registry.validate("grounded", "内部结论[1]，公开结论[2]。", "auto")
        self.assertEqual(["knowledge", "web"], [citation["type"] for citation in citations])
        json.dumps(citations)
        with self.assertRaises(AppError):
            registry.validate("clarification", "请补充信息", "auto")

    def test_v2_request_rejects_conflicting_web_scope(self):
        with self.assertRaises(ValueError):
            V2ChatRequest(question="查询", search_mode="web", document_ids=[uuid4()])

    def test_v2_response_accepts_web_citation(self):
        response = V2ChatResponse(
            message_id=uuid4(), answer="结论[1]", model_provider="deepseek", model_name="deepseek-chat",
            search_mode="web", citations=[{
                "type": "web", "number": 1, "title": "Docs", "url": "https://example.com",
                "excerpt": "内容", "retrieved_at": datetime.now(timezone.utc),
                "published_at": None, "score": None,
            }], token_usage=None, run_metadata={"schema_version": 1},
        )
        self.assertEqual("web", response.citations[0].type)

    def test_repository_isolates_sessions_by_user(self):
        engine = create_engine("sqlite://")
        connection = engine.connect()
        connection.execute(text("ATTACH DATABASE ':memory:' AS rag"))
        Base.metadata.create_all(connection)
        sessions = sessionmaker(bind=connection)
        with sessions() as database:
            first_user = User(external_id="first")
            second_user = User(external_id="second")
            database.add_all([first_user, second_user])
            database.flush()
            first_session = ChatSession(user_id=first_user.id, title="first")
            second_session = ChatSession(user_id=second_user.id, title="second")
            database.add_all([first_session, second_session])
            database.commit()
            self.assertEqual([first_session.id], [item.id for item in Repository(database).chat_sessions(first_user.id)])
            self.assertEqual([second_session.id], [item.id for item in Repository(database).chat_sessions(second_user.id)])
            with self.assertRaises(AppError):
                Repository(database).chat_session(first_user.id, second_session.id)

    def test_tavily_uses_fixed_endpoint_and_filters_unsafe_result(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [
                {"title": "bad", "url": "http://127.0.0.1/private", "content": "secret", "score": 1},
                {"title": "good", "url": "https://example.com/a", "content": "public", "score": 0.7},
            ], "usage": {"credits": 1}})
        settings = Settings(_env_file=None, tavily_api_key="token", web_search_enabled=True)
        search = TavilySearch(settings, transport=httpx.MockTransport(handler))
        batch = search.search("public query", 5)
        self.assertEqual("https://api.tavily.com/search", str(requests[0].url))
        self.assertEqual(["https://example.com/a"], [item.url for item in batch.results])


if __name__ == "__main__":
    unittest.main()
