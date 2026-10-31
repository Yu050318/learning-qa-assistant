"""Shared identity for the configured retrieval index, excluding rotating tokens."""
import hashlib
import json
import re
from dataclasses import dataclass, fields
from uuid import UUID


def retrieval_index_identity(settings):
    fields = ("milvus_uri", "milvus_database", "milvus_collection", "embedding_model", "embedding_dimension")
    payload = {name: getattr(settings, name) for name in fields}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def index_connection_identity(settings):
    payload = {name: getattr(settings, name) for name in ("milvus_uri", "milvus_database", "dashscope_base_url")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RetrievalIndex:
    document_id: UUID
    user_id: UUID
    collection_name: str
    database_name: str
    embedding_model: str
    embedding_dimension: int
    connection_sha256: str
    build_fingerprint: str
    source_sha256: str
    leaf_sha256: str
    leaf_count: int

    def __post_init__(self):
        if not isinstance(self.document_id, UUID) or not isinstance(self.user_id, UUID):
            raise ValueError("Index scope requires UUID identities")
        for name, limit in (("collection_name", 160), ("database_name", 128)):
            value = getattr(self, name)
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None or len(value) > limit:
                raise ValueError("Invalid index target")
        if not isinstance(self.embedding_model, str) or re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", self.embedding_model) is None:
            raise ValueError("Invalid embedding model")
        if type(self.embedding_dimension) is not int or not 64 <= self.embedding_dimension <= 4096:
            raise ValueError("Invalid index dimension")
        if type(self.leaf_count) is not int or not 1 <= self.leaf_count <= 100000:
            raise ValueError("Invalid index leaf count")
        for name in ("connection_sha256", "source_sha256", "leaf_sha256", "build_fingerprint"):
            value = getattr(self, name)
            if name == "build_fingerprint" and value == "legacy":
                continue
            if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
                raise ValueError("Invalid index identity digest")

    @classmethod
    def from_payload(cls, value):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("Invalid frozen index fields")
        return cls(**{**value, "document_id": UUID(str(value["document_id"])), "user_id": UUID(str(value["user_id"]))})
