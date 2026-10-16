import json
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from time import monotonic
from uuid import UUID

from pymilvus import AnnSearchRequest, DataType, Function, FunctionType, MilvusClient, RRFRanker
from pymilvus.exceptions import MilvusException

from app.core.config import Settings
from app.core.errors import AppError, UpstreamError
from app.domain.contracts import Chunk, ExcelSource, ParentContext, SearchHit


def user_filter(user_id: UUID, document_ids: list[UUID] | None = None) -> str:
    expression = f"user_id == {json.dumps(str(UUID(str(user_id))))}"
    if document_ids is not None:
        identifiers = [str(UUID(str(document_id))) for document_id in document_ids]
        expression += f" and document_id in {json.dumps(identifiers)}"
    return expression


def decode_chunk(entity: dict) -> Chunk:
    """One decoder for search hits and their context siblings."""
    try:
        entity = dict(entity)
        entity["page_number"] = entity.get("page_number") or None
        entity["section"] = entity.get("section") or None
        source = entity.get("excel_source")
        entity["excel_source"] = ExcelSource(**source) if source is not None else None
        chunk = Chunk(**entity)
        if type(chunk.chunk_index) is not int or chunk.chunk_index < 0 or not isinstance(chunk.content, str):
            raise ValueError
        return chunk
    except (TypeError, ValueError):
        raise AppError("VECTOR_METADATA_INVALID", "向量检索返回无效的切片或 Excel 来源", 503) from None


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

    def read_document_chunks(self, user_id: UUID, document_id: UUID, expected_count: int) -> list[Chunk]:
        """Explicit hierarchy backfill reads source leaves without writing vectors."""
        if not 1 <= expected_count <= self.settings.max_document_chunks:
            raise AppError("HIERARCHY_STALE", "文档切片数量无效", 409)
        deadline = monotonic() + self.settings.milvus_timeout
        leaves = []
        with self.connection() as client:
            fields = self._validate(client)
            output = [name for name in Chunk.__dataclass_fields__ if name != "excel_source"] + (["excel_source"] if "excel_source" in fields else [])
            for start in range(0, expected_count, 500):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise AppError("HIERARCHY_TIMEOUT", "补建读取切片超时", 504)
                end = min(start + 500, expected_count)
                rows = client.query(self.collection, filter=f"{user_filter(user_id, [document_id])} and chunk_index >= {start} and chunk_index < {end}",
                                    output_fields=output, limit=end - start + 1, consistency_level="Strong", timeout=remaining)
                batch = [decode_chunk(row) for row in rows]
                if (any(c.user_id != str(user_id) or c.document_id != str(document_id) for c in batch)
                        or sorted(c.chunk_index for c in batch) != list(range(start, end))):
                    raise AppError("HIERARCHY_SCOPE_MISMATCH", "补建切片来源、数量或序号不符", 503)
                leaves.extend(sorted(batch, key=lambda c: c.chunk_index))
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AppError("HIERARCHY_TIMEOUT", "补建读取切片超时", 504)
            overflow = client.query(self.collection, filter=f"{user_filter(user_id, [document_id])} and (chunk_index < 0 or chunk_index >= {expected_count})",
                                    output_fields=["chunk_id"], limit=1, consistency_level="Strong", timeout=remaining)
            if overflow or len({c.chunk_id for c in leaves}) != expected_count:
                raise AppError("HIERARCHY_STALE", "原有向量切片总数与文档记录不符", 409)
        return leaves

    def expand_context(self, user_id: UUID, document_ids: list[UUID], hits: list[SearchHit], *, deadline=None) -> list[SearchHit]:
        """Read bounded neighboring leaves; parents are derived, never separately indexed."""
        if not hits:
            return []
        group_size = self.settings.retrieval_parent_group_size
        deadline = min(deadline, monotonic() + self.settings.milvus_timeout) if deadline is not None else monotonic() + self.settings.milvus_timeout
        allowed = {str(document_id) for document_id in document_ids}
        groups = {}
        expanded = []

        def timeout():
            left = deadline - monotonic()
            if left <= 0:
                raise AppError("PARENT_CONTEXT_TIMEOUT", "父块上下文读取超时", 504)
            return left

        with self.connection() as client:
            fields = self._validate(client)
            output_fields = [name for name in Chunk.__dataclass_fields__ if name != "excel_source"] + (["excel_source"] if "excel_source" in fields else [])
            for hit in hits:
                leaf = hit.chunk
                if leaf.user_id != str(user_id) or leaf.document_id not in allowed:
                    raise AppError("VECTOR_SCOPE_MISMATCH", "父块检索范围无效", 503)
                if type(leaf.chunk_index) is not int or leaf.chunk_index < 0:
                    raise AppError("VECTOR_METADATA_INVALID", "切片位置无效", 503)
                if leaf.source_name.lower().endswith((".xls", ".xlsx")) and leaf.excel_source is None and not leaf.section:
                    expanded.append(replace(hit, context_warning="PARENT_CONTEXT_BOUNDARY_UNKNOWN"))
                    continue
                # ponytail: derive two-level parents from existing indices; persist a hierarchy only if evaluation warrants it.
                start = leaf.chunk_index // group_size * group_size
                key = (leaf.document_id, start)
                if key not in groups:
                    rows = client.query(self.collection,
                                        filter=f"{user_filter(user_id, [UUID(leaf.document_id)])} and chunk_index >= {start} and chunk_index < {start + group_size}",
                                        output_fields=output_fields, limit=group_size + 1,
                                        consistency_level="Strong", timeout=timeout())
                    siblings = [decode_chunk(row) for row in rows]
                    if any(child.user_id != str(user_id) or child.document_id != leaf.document_id
                           or not start <= child.chunk_index < start + group_size for child in siblings):
                        raise AppError("VECTOR_SCOPE_MISMATCH", "父块读取返回了范围外的切片", 503)
                    if len(siblings) > group_size or len({child.chunk_index for child in siblings}) != len(siblings):
                        raise AppError("VECTOR_METADATA_INVALID", "父块读取返回重复的切片位置", 503)
                    groups[key] = sorted(siblings, key=lambda child: child.chunk_index)
                siblings = groups[key]
                if leaf not in siblings:
                    expanded.append(replace(hit, context_warning="PARENT_CONTEXT_STALE"))
                    continue
                children = tuple(child for child in siblings if ParentContext.boundary(child) == ParentContext.boundary(leaf))
                if len(children) < 2:
                    expanded.append(hit)
                elif sum(len(child.content) for child in children) + 2 * (len(children) - 1) > 12000:
                    expanded.append(replace(hit, context_warning="PARENT_CONTEXT_TOO_LARGE"))
                else:
                    expanded.append(replace(hit, parent_context=ParentContext(children)))
            timeout()
        return expanded

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
            hits.append(SearchHit(decode_chunk(entity), float(match["distance"])))
        return hits
