"""Bounded JSON values used by durable execution."""
import json
from uuid import UUID
from datetime import datetime
from app.core.errors import AppError

def checkpoint_json_value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Checkpoint values must be JSON data, UUIDs or timestamps")


def bounded_json(value, max_bytes):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=checkpoint_json_value)
    if len(encoded.encode()) > max_bytes:
        raise AppError("RUN_CHECKPOINT_LIMIT", "运行检查点超过保存上限", 413)
    return json.loads(encoded)
