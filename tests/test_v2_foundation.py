import unittest
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
from pymilvus import DataType, MilvusClient
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
from app.infrastructure.search.scrapling import ScraplingWebReader
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

    def test_milvus_hybrid_search_scopes_dense_and_bm25_requests(self):
        settings = Settings(_env_file=None, embedding_dimension=64)
        store = MilvusVectorStore(settings)
        user_id, document_id, chunk_id = uuid4(), uuid4(), uuid4()
        client = MagicMock()
        client.hybrid_search.return_value = [[{
            "entity": {
                "chunk_id": str(chunk_id), "user_id": str(user_id),
                "document_id": str(document_id), "chunk_index": 0,
                "content": "错误码 ERR_XQ_731", "source_name": "manual.md",
                "page_number": 0, "section": "",
            },
            "distance": 0.03,
        }]]
        connection = MagicMock()
        connection.__enter__.return_value = client

        with patch.object(store, "connection", return_value=connection), patch.object(store, "_validate"):
            hits = store.search(user_id, [document_id], "ERR_XQ_731", [0.0] * 64, 12)

        request = client.hybrid_search.call_args
        dense, sparse = request.kwargs["reqs"]
        expected_scope = f'user_id == "{user_id}" and document_id in ["{document_id}"]'
        self.assertEqual(["vector", "sparse_vector"], [dense.anns_field, sparse.anns_field])
        self.assertEqual([expected_scope, expected_scope], [dense.filter, sparse.filter])
        self.assertEqual(["ERR_XQ_731"], sparse.data)
        self.assertEqual(str(chunk_id), hits[0].chunk.chunk_id)
        self.assertEqual(0.03, hits[0].score)

    def test_milvus_initializes_native_bm25_schema(self):
        settings = Settings(_env_file=None, embedding_dimension=64)
        store = MilvusVectorStore(settings)
        default_client, client = MagicMock(), MagicMock()
        default_client.list_databases.return_value = [settings.milvus_database]
        client.has_collection.return_value = False
        client.prepare_index_params.side_effect = MilvusClient.prepare_index_params
        client.describe_collection.return_value = {
            "description": store.memory_signature,
            "fields": [{"name": "vector", "params": {"dim": 64}}],
        }
        default_connection, connection = MagicMock(), MagicMock()
        default_connection.__enter__.return_value = default_client
        connection.__enter__.return_value = client

        with patch.object(store, "connection", side_effect=[default_connection, connection]), patch.object(store, "_validate"):
            store.initialize()

        created = client.create_collection.call_args_list[0].kwargs
        schema = created["schema"].to_dict()
        fields = {field["name"]: field for field in schema["fields"]}
        self.assertIn("sparse_vector", fields)
        self.assertEqual(DataType.JSON, fields["excel_source"]["type"])
        self.assertTrue(fields["excel_source"]["nullable"])
        self.assertTrue(fields["content"]["params"]["enable_analyzer"])
        self.assertEqual("content_bm25", schema["functions"][0]["name"])
        sparse_index = next(index.to_dict() for index in created["index_params"] if index.field_name == "sparse_vector")
        self.assertEqual("BM25", sparse_index["metric_type"])

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
        self.assertEqual([], registry.validate("clarification", "请补充信息", "auto"))
        with self.assertRaises(AppError):
            registry.validate("clarification", "请补充信息[1]", "auto")

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
            first_user = User(username="first", password_hash="hash")
            second_user = User(username="second", password_hash="hash")
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

    def test_scrapling_reader_rejects_private_dns_result(self):
        fetched = []
        reader = ScraplingWebReader(
            Settings(_env_file=None),
            fetch=lambda *args, **kwargs: fetched.append((args, kwargs)),
            resolve=lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
        )

        with self.assertRaises(AppError) as raised:
            reader.read("https://internal.example/page", 5)

        self.assertEqual("UNSAFE_WEB_URL", raised.exception.code)
        self.assertEqual([], fetched)

    def test_scrapling_reader_returns_clean_markdown(self):
        requests = []

        class Selection:
            def get(self):
                return "Example documentation"

        class Page:
            url = "https://example.com/docs"
            status = 200
            headers = {"content-type": "text/html; charset=utf-8"}
            body = b"<html><body>content</body></html>"

            def markdown(self, main_content_only=False):
                self.main_content_only = main_content_only
                return "# Clean content"

            def css(self, selector):
                return Selection()

        page = Page()

        def fetch(url, **kwargs):
            requests.append((url, kwargs))
            return page

        reader = ScraplingWebReader(
            Settings(_env_file=None, scrapling_timeout=7), fetch=fetch,
            resolve=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
        )

        result = reader.read("https://example.com/docs", 10)

        self.assertEqual("# Clean content", result.content)
        self.assertEqual("Example documentation", result.title)
        self.assertTrue(page.main_content_only)
        self.assertLessEqual(requests[0][1]["timeout"], 7)
        self.assertFalse(requests[0][1]["follow_redirects"])

    def test_scrapling_validates_redirect_before_connecting_to_target(self):
        from types import SimpleNamespace
        fetched = []

        def fetch(url, **kwargs):
            fetched.append(url)
            return SimpleNamespace(url=url, status=302, headers={"Location": "http://127.0.0.1/secret"})

        reader = ScraplingWebReader(Settings(_env_file=None), fetch=fetch,
                                   resolve=lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))])
        with self.assertRaises(AppError) as raised:
            reader.read("https://example.com/docs", 5)
        self.assertEqual("UNSAFE_WEB_URL", raised.exception.code)
        self.assertEqual(["https://example.com/docs"], fetched)

    def test_scrapling_transport_pins_dns_and_bounds_download_before_parsing(self):
        import ipaddress
        from types import SimpleNamespace
        from unittest.mock import patch, MagicMock
        from curl_cffi import CurlOpt

        response = SimpleNamespace(url="https://example.com/docs", status_code=200, reason="OK", encoding="utf-8",
                                   headers={"content-type": "text/html"})
        session = MagicMock()

        def get(url, **kwargs):
            kwargs["content_callback"](b"<html><title>Docs</title><main>Public content</main></html>")
            return response

        session.get.side_effect = get
        with patch("app.infrastructure.search.scrapling.Session") as factory:
            factory.return_value.__enter__.return_value = session
            result = ScraplingWebReader._fetch_pinned("https://example.com/docs", {ipaddress.ip_address("93.184.216.34")}, 5)
            self.assertIn("Public content", result.markdown())
            self.assertEqual([b"example.com:443:93.184.216.34"], factory.call_args.kwargs["curl_options"][CurlOpt.RESOLVE])
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

            def too_big(url, **kwargs):
                self.assertEqual(0, kwargs["content_callback"](b"x" * (2 * 1024 * 1024 + 1)))
                raise RuntimeError("aborted")

            session.get.side_effect = too_big
            with self.assertRaises(AppError) as raised:
                ScraplingWebReader._fetch_pinned("https://example.com/docs", {ipaddress.ip_address("93.184.216.34")}, 5)
            self.assertEqual("WEB_PAGE_TOO_LARGE", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
