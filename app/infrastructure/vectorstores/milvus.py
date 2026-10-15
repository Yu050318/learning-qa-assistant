import json
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict
from uuid import UUID

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker
from pymilvus.exceptions import MilvusException

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError
from app.domain.contracts import Chunk, ExcelSource, SearchHit


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
        self.signature = f"rag-hybrid:v1:{settings.embedding_model}:{settings.embedding_dimension}:chinese"
        self.memory_collection = settings.memory_collection
        self.memory_signature = f"rag-memory:{settings.embedding_model}:{settings.embedding_dimension}"

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

    def _validate(self, client: MilvusClient) -> set[str]:
        description = client.describe_collection(self.collection, timeout=self.settings.milvus_timeout)
        fields = {field["name"]: field for field in description["fields"]}
        dimension = int(fields.get("vector", {}).get("params", {}).get("dim", 0))
        content_params = fields.get("content", {}).get("params", {})
        functions = description.get("functions") or []
        has_bm25 = any(
            function.get("name") == "content_bm25"
            and function.get("input_field_names") == ["content"]
            and function.get("output_field_names") == ["sparse_vector"]
            for function in functions
        )
        if (
            description.get("description") != self.signature
            or dimension != self.settings.embedding_dimension
            or "sparse_vector" not in fields
            or not content_params.get("enable_analyzer")
            or not has_bm25
        ):
            raise AppError("VECTOR_SCHEMA_MISMATCH", "Milvus 集合与当前模型或维度不兼容，请创建新集合并重新入库", 503)
        if "excel_source" in fields and fields["excel_source"].get("type") != DataType.JSON:
            raise AppError("VECTOR_SCHEMA_MISMATCH", "Excel 来源字段必须是 JSON 类型", 503)
        return set(fields)

    def add_excel_provenance(self) -> None:
        """Explicit, additive migration; never delete/recreate a collection."""
        with self.connection() as client:
            fields = self._validate(client)
            if "excel_source" not in fields:
                client.add_collection_field(self.collection, "excel_source", DataType.JSON,
                                            nullable=True, timeout=self.settings.milvus_timeout)
                if "excel_source" not in self._validate(client):
                    raise AppError("VECTOR_SCHEMA_MISMATCH", "Excel 来源字段升级后尚不可用", 503)

    def initialize(self) -> None:
        with self.connection(default_database=True) as client:
            if self.settings.milvus_database not in client.list_databases(timeout=self.settings.milvus_timeout):
                client.create_database(db_name=self.settings.milvus_database, timeout=self.settings.milvus_timeout)
        with self.connection() as client:
            if not client.has_collection(self.collection, timeout=self.settings.milvus_timeout):
                schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False, description=self.signature)
                schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=36)
                schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension)
                schema.add_field(
                    "content", DataType.VARCHAR, max_length=65535,
                    enable_analyzer=True, analyzer_params={"type": "chinese"},
                )
                schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
                schema.add_function(Function(
                    name="content_bm25", function_type=FunctionType.BM25,
                    input_field_names=["content"], output_field_names=["sparse_vector"],
                ))
                for name, maximum in (("user_id", 36), ("document_id", 36), ("source_name", 1024), ("section", 2048), ("embedding_model", 200)):
                    schema.add_field(name, DataType.VARCHAR, max_length=maximum)
                schema.add_field("chunk_index", DataType.INT64)
                schema.add_field("page_number", DataType.INT64)
                schema.add_field("excel_source", DataType.JSON, nullable=True)
                indexes = client.prepare_index_params()
                indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
                indexes.add_index(
                    field_name="sparse_vector", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25",
                    params={"inverted_index_algo": "DAAT_MAXSCORE"},
                )
                for name in ("user_id", "document_id"):
                    indexes.add_index(field_name=name, index_type="INVERTED")
                client.create_collection(collection_name=self.collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=self.settings.milvus_timeout)
            self._validate(client)
            client.load_collection(self.collection, timeout=self.settings.milvus_timeout)
            if not client.has_collection(self.memory_collection, timeout=self.settings.milvus_timeout):
                schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False, description=self.memory_signature)
                schema.add_field("memory_id", DataType.VARCHAR, is_primary=True, max_length=36)
                schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.settings.embedding_dimension)
                schema.add_field("user_id", DataType.VARCHAR, max_length=36)
                schema.add_field("version", DataType.INT64)
                indexes = client.prepare_index_params()
                indexes.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
                indexes.add_index(field_name="user_id", index_type="INVERTED")
                client.create_collection(collection_name=self.memory_collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=self.settings.milvus_timeout)
            description = client.describe_collection(self.memory_collection, timeout=self.settings.milvus_timeout)
            dimension = int({field["name"]: field for field in description["fields"]}.get("vector", {}).get("params", {}).get("dim", 0))
            if description.get("description") != self.memory_signature or dimension != self.settings.embedding_dimension:
                raise AppError("VECTOR_SCHEMA_MISMATCH", "长期记忆集合与当前向量模型不兼容", 503)
            client.load_collection(self.memory_collection, timeout=self.settings.milvus_timeout)

    def check(self) -> None:
        with self.connection() as client:
            self._validate(client)
            client.describe_collection(self.memory_collection, timeout=self.settings.milvus_timeout)

    def upsert_memory(self, user_id: UUID, memory_id: UUID, version: int, vector: list[float]) -> None:
        if len(vector) != self.settings.embedding_dimension:
            raise AppError("EMBEDDING_DIMENSION_MISMATCH", "向量维度不一致", 502)
        with self.connection() as client:
            client.upsert(self.memory_collection, data=[{
                "memory_id": str(memory_id), "user_id": str(user_id), "version": version, "vector": vector,
            }], timeout=self.settings.milvus_timeout)

    def delete_memory(self, user_id: UUID, memory_id: UUID) -> None:
        expression = f'user_id == {json.dumps(str(user_id))} and memory_id == {json.dumps(str(memory_id))}'
        with self.connection() as client:
            client.delete(self.memory_collection, filter=expression, timeout=self.settings.milvus_timeout)

    def search_memories(self, user_id: UUID, vector: list[float], top_k: int) -> list[dict]:
        with self.connection() as client:
            result = client.search(
                self.memory_collection, data=[vector], anns_field="vector",
                filter=f'user_id == {json.dumps(str(user_id))}', limit=top_k,
                output_fields=["memory_id", "user_id", "version"],
                search_params={"metric_type": "COSINE", "params": {}},
                consistency_level="Strong", timeout=self.settings.milvus_timeout,
            )
        hits = []
        for match in result[0]:
            entity = match["entity"]
            if entity["user_id"] != str(user_id):
                raise AppError("VECTOR_SCOPE_MISMATCH", "长期记忆检索返回了范围外的结果", 503)
            hits.append({**entity, "score": float(match["distance"])})
        return hits

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
            fields = self._validate(client)
            if "excel_source" not in fields:
                if any(chunk.excel_source is not None for chunk in chunks):
                    raise AppError("VECTOR_PROVENANCE_REQUIRED", "请先运行 app.bootstrap --excel-provenance，为现有集合增加来源字段", 503)
                for row in rows:
                    row.pop("excel_source", None)
            for offset in range(0, len(rows), 100):
                client.upsert(self.collection, data=rows[offset:offset + 100], timeout=self.settings.milvus_timeout)

    def delete_document(self, user_id: UUID, document_id: UUID) -> None:
        with self.connection() as client:
            self._validate(client)
            client.delete(self.collection, filter=user_filter(user_id, [document_id]), timeout=self.settings.milvus_timeout)

    def search(self, user_id: UUID, document_ids: list[UUID], query: str, vector: list[float], top_k: int) -> list[SearchHit]:
        with self.connection() as client:
            fields = self._validate(client)
            if not document_ids:
                return []
            scope = user_filter(user_id, document_ids)
            dense = AnnSearchRequest(
                data=[vector], anns_field="vector", param={"metric_type": "COSINE", "params": {}},
                limit=top_k, filter=scope,
            )
            sparse = AnnSearchRequest(
                data=[query], anns_field="sparse_vector", param={"metric_type": "BM25", "params": {}},
                limit=top_k, filter=scope,
            )
            result = client.hybrid_search(
                self.collection, reqs=[dense, sparse], ranker=RRFRanker(60), limit=top_k,
                output_fields=[name for name in Chunk.__dataclass_fields__ if name != "excel_source"] + (["excel_source"] if "excel_source" in fields else []),
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
            source = entity.get("excel_source")
            try:
                entity["excel_source"] = ExcelSource(**source) if source is not None else None
            except (TypeError, ValueError):
                raise AppError("VECTOR_METADATA_INVALID", "向量检索返回无效的 Excel 来源", 503) from None
            hits.append(SearchHit(Chunk(**entity), float(match["distance"])))
        return hits
