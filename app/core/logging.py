import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "operation": record.getMessage(),
        }
        for key in ("request_id", "user_hash", "resource_id", "elapsed_ms", "error_type", "model_provider", "retrieved_count", "status_code"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    logger = logging.getLogger("rag")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for name in ("httpx", "httpcore", "sqlalchemy.engine", "pymilvus", "langsmith"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
