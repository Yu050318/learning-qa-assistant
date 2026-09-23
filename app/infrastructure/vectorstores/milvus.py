import json
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict
from uuid import UUID

from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError
from app.domain.contracts import Chunk, SearchHit


def user_filter(user_id: UUID, document_ids: list[UUID] | None = None) -> str:
    expression = f"user_id == {json.dumps(str(UUID(str(user_id))))}"
    if document_ids is not None:
        identifiers = [str(UUID(str(document_id))) for document_id in document_ids]
        expression += f" and document_id in {json.dumps(identifiers)}"
    return expression


class MilvusVectorStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.collection = settings.milvus_collection
        self.signature = f"rag:{settings.embedding_model}:{settings.embedding_dimension}"

    @contextmanager
    def connection(self, default_database: bool = False):
        client = None
        try:
            connection_options = {
                "uri": self.settings.milvus_uri,
                "db_name": "default" if default_database else self.settings.milvus_database,
                "timeout": self.settings.milvus_timeout,
            }
            token = self.settings.milvus_token.get_secret_value()
            if token:
                connection_options["token"] = token
            client = MilvusClient(**connection_options)
            yield client
        except MilvusException:
            raise UpstreamError("milvus") from None
        finally:
            if client is not None:
                client.close()

    def _validate(self, client: MilvusClient) -> None:
        description = client.describe_collection(self.collection, timeout=self.settings.milvus_timeout)
        fields = {field["name"]: field for field in description["fields"]}
        dimension = int(fields.get("vector", {}).get("params", {}).get("dim", 0))
        if description.get("description") != self.signature or dimension != self.settings.embedding_dimension:
            raise AppError("VECTOR_SCHEMA_MISMATCH", "Milvus 集合与当前模型或维度不兼容，请创建新集合并重新入库", 503)

    def initialize(self) -> None:
        with self.connection(default_database=True) as client:
            if self.settings.milvus_database not in client.list_databases(timeout=self.settings.milvus_timeout):
                client.create_database(db_name=self.settings.milvus_database, timeout=self.settings.milvus_timeout)
        with self.connection() as client:
            if not client.has_collection(self.collection, timeout=self.settings.milvus_timeout):
                schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False, description=self.signature)
                schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=36)
                schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension)
                for name, maximum in (("user_id", 36), ("document_id", 36), ("content", 65535), ("source_name", 1024), ("section", 2048), ("embedding_model", 200)):
                    schema.add_field(name, DataType.VARCHAR, max_length=maximum)
                schema.add_field("chunk_index", DataType.INT64)
                schema.add_field("page_number", DataType.INT64)
                indexes = client.prepare_index_params()
                indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
                for name in ("user_id", "document_id"):
                    indexes.add_index(field_name=name, index_type="INVERTED")
                client.create_collection(collection_name=self.collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=self.settings.milvus_timeout)
            self._validate(client)
            client.load_collection(self.collection, timeout=self.settings.milvus_timeout)

    def check(self) -> None:
        with self.connection() as client:
            self._validate(client)

    def upsert(self, chunks: Sequence[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise AppError("EMBEDDING_COUNT_MISMATCH", "切片与向量数量不一致", 502)
        if any(len(vector) != self.settings.embedding_dimension for vector in vectors):
            raise AppError("EMBEDDING_DIMENSION_MISMATCH", "向量维度不一致", 502)
        rows = [
            {**asdict(chunk), "vector": vector, "page_number": chunk.page_number or 0,
             "section": chunk.section or "", "embedding_model": self.settings.embedding_model}
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        with self.connection() as client:
            self._validate(client)
            for offset in range(0, len(rows), 100):
                client.upsert(self.collection, data=rows[offset:offset + 100], timeout=self.settings.milvus_timeout)

    def delete_document(self, user_id: UUID, document_id: UUID) -> None:
        with self.connection() as client:
            self._validate(client)
            client.delete(self.collection, filter=user_filter(user_id, [document_id]), timeout=self.settings.milvus_timeout)

    def search(self, user_id: UUID, document_ids: list[UUID], vector: list[float], top_k: int) -> list[SearchHit]:
        with self.connection() as client:
            self._validate(client)
            if not document_ids:
                return []
            result = client.search(
                self.collection, data=[vector], anns_field="vector", filter=user_filter(user_id, document_ids),
                limit=top_k, output_fields=list(Chunk.__dataclass_fields__),
                search_params={"metric_type": "COSINE", "params": {}},
                consistency_level="Strong", timeout=self.settings.milvus_timeout,
            )
        allowed = {str(document_id) for document_id in document_ids}
        hits = []
        for match in result[0]:
            entity = match["entity"]
            if entity["user_id"] != str(user_id) or entity["document_id"] not in allowed:
                raise AppError("VECTOR_SCOPE_MISMATCH", "向量检索返回了范围外的结果", 503)
            entity["page_number"] = entity["page_number"] or None
            entity["section"] = entity["section"] or None
            hits.append(SearchHit(Chunk(**entity), float(match["distance"])))
        return hits
