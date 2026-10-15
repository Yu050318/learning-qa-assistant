import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.application.auth import AuthContext
from app.main import create_app


class Sessions:
    def create(self, user_id, title):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(id=uuid4(), title=title, summary="", summarized_through=None,
                               created_at=now, updated_at=now)


class Chat:
    def answer_v2(self, user_id, session_id, payload, request_id, on_answer_event=None):
        if on_answer_event:
            on_answer_event("answer_start", "")
            on_answer_event("answer_delta", "你好")
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=uuid4(), content="你好", model_provider="deepseek", model_name="deepseek-chat",
            citations=[], token_usage=None, run_metadata={"schema_version": 1, "search_mode": "auto"},
            created_at=now,
        )


class Ingestion:
    def __init__(self):
        self.document = self._document()
        self.scheduled = []
        self.deleted = []

    @staticmethod
    def _document():
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=uuid4(), original_name="notes.txt", file_type=".txt", status="processing",
            chunk_count=0, embedding_model="text-embedding-v4", error_message=None,
            size_bytes=5, parser_metadata={}, created_at=now, updated_at=now,
        )

    def upload(self, user_id, filename, content_type, stream):
        self.document.original_name = filename
        return self.document

    def schedule(self, user_id, document_id):
        self.scheduled.append((user_id, document_id))

    def get(self, user_id, document_id):
        if document_id != self.document.id:
            raise AppError("NOT_FOUND", "资源不存在", 404)
        return self.document

    def retry(self, user_id, document_id):
        return self.get(user_id, document_id)

    def delete(self, user_id, document_id):
        self.get(user_id, document_id)
        self.deleted.append((user_id, document_id))


class Services:
    def __init__(self):
        self.settings = Settings(_env_file=None)
        self.sessions = Sessions()
        self.chat = Chat()
        self.ingestion = Ingestion()
        self.current_user = uuid4()
        self.auth = SimpleNamespace(authenticate=lambda token: AuthContext(self.current_user, "alice", uuid4()))

    def readiness(self):
        return {"status": "ready", "checks": {}}

    def close(self):
        pass


class ApiV2Tests(unittest.TestCase):
    headers = {"Authorization": "Bearer test"}

    def test_agent_events_and_sql_citation_roundtrip(self):
        services = Services()
        original = services.chat.answer_v2
        received = []

        def answer(user_id, session_id, payload, request_id, on_answer_event=None):
            received.append(payload)
            if on_answer_event:
                on_answer_event("agent_started", {"agent": "sql", "status": "running"})
                on_answer_event("agent_finished", {"agent": "sql", "status": "ok"})
            message = original(user_id, session_id, payload, request_id, on_answer_event)
            message.citations = [{"type": "sql", "number": 1, "source_name": "业务库", "query": "SELECT 1",
                                  "columns": ["value"], "row_count": 1, "truncated": False, "excerpt": "1"}]
            message.run_metadata = {"search_mode": "sql"}
            return message

        services.chat.answer_v2 = answer
        with TestClient(create_app(Settings(_env_file=None), services)) as client:
            response = client.post(f"/api/v2/sessions/{uuid4()}/messages/stream", headers=self.headers,
                                   json={"question": "销量", "search_mode": "sql"})
            history_contract = client.post(f"/api/v2/sessions/{uuid4()}/messages", headers=self.headers,
                                           json={"question": "销量", "search_mode": "sql"})
        self.assertEqual("sql", received[0].search_mode)
        self.assertIn('"agent": "sql"', response.text)
        self.assertIn("event: agent_finished", response.text)
        self.assertEqual(200, history_contract.status_code)
        self.assertEqual("sql", history_contract.json()["citations"][0]["type"])

    def test_url_and_mode_validation(self):
        from app.api.schemas import V2ChatRequest, ContextEstimateRequest
        from pydantic import ValidationError
        for schema in (V2ChatRequest, ContextEstimateRequest):
            self.assertEqual(["https://example.com"], schema(question="读取", web_urls=["https://example.com"]).web_urls)
            for body in (
                {"search_mode": "sql", "web_urls": ["https://example.com"]},
                {"search_mode": "sql", "document_ids": [str(uuid4())]},
                {"web_urls": ["http://127.0.0.1"]},
                {"web_urls": ["https://example.com"] * 4},
            ):
                with self.subTest(body=body), self.assertRaises(ValidationError):
                    schema(question="查询", **body)

    def test_specified_urls_passed_to_workflow_input(self):
        services = Services()
        original = services.chat.answer_v2
        received = []

        def answer(user_id, session_id, payload, request_id, on_answer_event=None):
            received.append(payload)
            return original(user_id, session_id, payload, request_id, on_answer_event)

        services.chat.answer_v2 = answer
        with TestClient(create_app(Settings(_env_file=None), services)) as client:
            response = client.post(f"/api/v2/sessions/{uuid4()}/messages", headers=self.headers,
                                   json={"question": "读取", "search_mode": "web", "web_urls": ["https://example.com/docs"]})
        self.assertEqual(200, response.status_code)
        self.assertEqual(("https://example.com/docs",), received[0].web_urls)
    def test_v2_session_route_creates_v2_session(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post("/api/v2/sessions", headers=self.headers, json={"title": "V2"})
        self.assertEqual(201, response.status_code)
        self.assertEqual("V2", response.json()["title"])

    def test_v2_document_upload_schedules_ingestion(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post(
                "/api/v2/documents", headers=self.headers,
                files={"file": ("notes.txt", b"hello", "text/plain")},
            )
        self.assertEqual(202, response.status_code)
        self.assertEqual([(services.current_user, services.ingestion.document.id)], services.ingestion.scheduled)

    def test_v2_document_detail_uses_user_scope(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.get(
                f"/api/v2/documents/{services.ingestion.document.id}", headers=self.headers,
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(str(services.ingestion.document.id), response.json()["id"])

    def test_v2_document_retry_schedules_ingestion(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/documents/{services.ingestion.document.id}/retry",
                headers=self.headers,
            )
        self.assertEqual(202, response.status_code)
        self.assertEqual([(services.current_user, services.ingestion.document.id)], services.ingestion.scheduled)

    def test_v2_document_delete_returns_204(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.delete(
                f"/api/v2/documents/{services.ingestion.document.id}", headers=self.headers,
            )
        self.assertEqual(204, response.status_code)
        self.assertEqual([(services.current_user, services.ingestion.document.id)], services.ingestion.deleted)

    def test_v1_routes_return_404(self):
        app = create_app(Settings(_env_file=None), Services())
        with TestClient(app) as client:
            sessions = client.get("/api/v1/sessions", headers=self.headers)
            documents = client.get("/api/v1/documents", headers=self.headers)
        self.assertEqual(404, sessions.status_code)
        self.assertEqual(404, documents.status_code)

    def test_v2_chat_reads_search_mode_from_run_metadata(self):
        app = create_app(Settings(_env_file=None), Services())
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages", headers=self.headers,
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("auto", response.json()["search_mode"])

    def test_v2_stream_returns_ordered_sse_events_and_final_answer(self):
        app = create_app(Settings(_env_file=None), Services())
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages/stream", headers=self.headers,
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        blocks = [block for block in response.text.split("\n\n") if block]
        events = [next(line[7:] for line in block.splitlines() if line.startswith("event: ")) for block in blocks]
        values = [json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: "))) for block in blocks]
        self.assertEqual(["run_started", "answer_start", "answer_delta", "usage", "done"], events)
        self.assertEqual([1, 2, 3, 4, 5], [value["sequence"] for value in values])
        self.assertEqual("你好", values[2]["delta"])
        self.assertEqual("你好", values[-1]["answer"])
        self.assertEqual(values[0]["run_id"], values[-1]["run_id"])

    def test_v2_stream_encodes_business_errors_after_stream_starts(self):
        services = Services()
        services.chat.answer_v2 = lambda *args, **kwargs: (_ for _ in ()).throw(AppError("NOPE", "失败", 409))
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages/stream", headers=self.headers,
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertIn("event: error", response.text)
        self.assertIn('"code": "NOPE"', response.text)


if __name__ == "__main__":
    unittest.main()
