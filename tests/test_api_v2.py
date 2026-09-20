import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import AppError
from app.main import create_app


class Sessions:
    def create(self, user_id, title, *, api_version="v1"):
        self.version = api_version
        now = datetime.now(timezone.utc)
        return SimpleNamespace(id=uuid4(), title=title, summary="", summarized_through=None,
                               created_at=now, updated_at=now)


class Chat:
    def answer_v2(self, user_id, session_id, payload, request_id):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            id=uuid4(), content="你好", model_provider="deepseek", model_name="deepseek-chat",
            citations=[], token_usage=None, run_metadata={"schema_version": 1, "search_mode": "auto"},
            created_at=now,
        )


class Services:
    def __init__(self):
        self.sessions = Sessions()
        self.chat = Chat()

    def user_id(self, external_id):
        return uuid4()

    def readiness(self):
        return {"status": "ready", "checks": {}}

    def close(self):
        pass


class ApiV2Tests(unittest.TestCase):
    def test_v2_session_route_creates_v2_session(self):
        services = Services()
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post("/api/v2/sessions", headers={"X-User-ID": "alice"}, json={"title": "V2"})
        self.assertEqual(201, response.status_code)
        self.assertEqual("v2", services.sessions.version)

    def test_v2_chat_reads_search_mode_from_run_metadata(self):
        app = create_app(Settings(_env_file=None), Services())
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages", headers={"X-User-ID": "alice"},
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("auto", response.json()["search_mode"])

    def test_v2_stream_returns_ordered_sse_events_and_final_answer(self):
        app = create_app(Settings(_env_file=None), Services())
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages/stream", headers={"X-User-ID": "alice"},
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        blocks = [block for block in response.text.split("\n\n") if block]
        events = [next(line[7:] for line in block.splitlines() if line.startswith("event: ")) for block in blocks]
        values = [json.loads(next(line[6:] for line in block.splitlines() if line.startswith("data: "))) for block in blocks]
        self.assertEqual(["run_started", "usage", "done"], events)
        self.assertEqual([1, 2, 3], [value["sequence"] for value in values])
        self.assertEqual("你好", values[-1]["answer"])
        self.assertEqual(values[0]["run_id"], values[-1]["run_id"])

    def test_v2_stream_encodes_business_errors_after_stream_starts(self):
        services = Services()
        services.chat.answer_v2 = lambda *args: (_ for _ in ()).throw(AppError("NOPE", "失败", 409))
        app = create_app(Settings(_env_file=None), services)
        with TestClient(app) as client:
            response = client.post(
                f"/api/v2/sessions/{uuid4()}/messages/stream", headers={"X-User-ID": "alice"},
                json={"question": "你好", "search_mode": "auto"},
            )
        self.assertEqual(200, response.status_code)
        self.assertIn("event: error", response.text)
        self.assertIn('"code": "NOPE"', response.text)


if __name__ == "__main__":
    unittest.main()
