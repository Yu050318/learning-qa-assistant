import json
import logging
import asyncio
from queue import Empty, Queue
from threading import Thread
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.dependencies import AdminDependency, AuthDependency, ServiceDependency, UserDependency
from app.api.schemas import (
    AuthResponse, CredentialsRequest, RegisterRequest, DocumentResponse, MemoryResponse, MemoryWrite, SessionCreate, SessionResponse, SessionRename, UserResponse,
    V2ChatRequest, V2ChatResponse, V2MessageResponse, V2SessionDetail,
    CompactRequest, ContextEstimateRequest, DocumentPage, ActivateDocumentVersion,
)
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.infrastructure.persistence.models import ChatRun
from app.infrastructure.persistence.repository import Repository

v2_router = APIRouter(prefix="/api/v2")
health_router = APIRouter(tags=["health"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]
logger = logging.getLogger("rag")
REFRESH_COOKIE = "rag_refresh"


def auth_payload(user, access: str, seconds: int) -> dict:
    return {"access_token": access, "token_type": "bearer", "expires_in": seconds,
            "user": {"id": user.id, "username": user.username, "role": user.role}}


def set_refresh_cookie(response: Response, services, value: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE, value, max_age=services.settings.refresh_token_days * 86400,
        httponly=True, secure=services.settings.auth_cookie_secure, samesite="strict", path="/api/v2/auth",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def verify_cookie_origin(request: Request, services, origin: str | None, sec_fetch_site: str | None) -> None:
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    services.auth.verify_cookie_origin(origin, sec_fetch_site, request_origin)


@v2_router.post("/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"])
def register(payload: RegisterRequest, request: Request, response: Response, services: ServiceDependency):
    user, access, refresh, seconds = services.auth.register(payload.username, payload.password, payload.admin_code,
        request.client.host if request.client else "unknown")
    set_refresh_cookie(response, services, refresh)
    return auth_payload(user, access, seconds)


@v2_router.post("/auth/login", response_model=AuthResponse, tags=["auth"])
def login(payload: CredentialsRequest, request: Request, response: Response, services: ServiceDependency):
    user, access, refresh, seconds = services.auth.login(payload.username, payload.password, request.client.host if request.client else "unknown")
    set_refresh_cookie(response, services, refresh)
    return auth_payload(user, access, seconds)


@v2_router.post("/auth/refresh", response_model=AuthResponse, tags=["auth"])
def refresh_auth(request: Request, response: Response, services: ServiceDependency,
                 refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
                 origin: Annotated[str | None, Header()] = None,
                 sec_fetch_site: Annotated[str | None, Header(alias="Sec-Fetch-Site")] = None):
    verify_cookie_origin(request, services, origin, sec_fetch_site)
    if not refresh:
        raise AppError("UNAUTHORIZED", "刷新凭据缺失", 401)
    user, access, new_refresh, seconds = services.auth.refresh(refresh)
    set_refresh_cookie(response, services, new_refresh)
    return auth_payload(user, access, seconds)


@v2_router.get("/auth/me", response_model=UserResponse, tags=["auth"])
def me(context: AuthDependency):
    return {"id": context.user_id, "username": context.username, "role": context.role}


@v2_router.post("/auth/logout", status_code=204, tags=["auth"])
def logout(response: Response, services: ServiceDependency, context: AuthDependency):
    services.auth.logout(context)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v2/auth")
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 204
    return response


@v2_router.get("/admin/overview", tags=["administration"])
def admin_overview(services: ServiceDependency, user_id: AdminDependency, response: Response):
    response.headers["Cache-Control"] = "no-store"
    settings = services.settings
    return {"features": {
        "model_center": settings.model_center_enabled,
        "sharing": settings.sharing_enabled,
        "document_versions": settings.document_versions_enabled,
        "durable_runs": settings.chat_runs_enabled,
        "hierarchy": settings.hierarchy_enabled,
        "knowledge_quality": settings.knowledge_quality_enabled,
        "rerank": settings.rerank_enabled,
    }, "ingestion_mode": settings.ingestion_mode,
        "notice": "此处显示配置状态，服务可用性需通过部署预检与实际运行确认。"}


@v2_router.get("/admin/preflight", tags=["administration"])
def admin_preflight(services: ServiceDependency, user_id: AdminDependency, response: Response):
    from app.application.preflight import deployment_preflight
    response.headers["Cache-Control"] = "no-store"
    return deployment_preflight(services.settings, services.database, services.vectors)


@v2_router.post("/auth/logout-all", status_code=204, tags=["auth"])
def logout_all(response: Response, services: ServiceDependency, context: AuthDependency):
    services.auth.logout_all(context)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v2/auth")
    response.headers["Cache-Control"] = "no-store"
    response.status_code = 204
    return response


@v2_router.get("/memories", response_model=list[MemoryResponse], tags=["memories"])
def list_memories(services: ServiceDependency, user_id: UserDependency):
    return services.memories.list(user_id)


@v2_router.post("/memories", response_model=MemoryResponse, status_code=201, tags=["memories"])
def create_memory(payload: MemoryWrite, services: ServiceDependency, user_id: UserDependency):
    return services.memories.create(user_id, payload.content, created_via="management_ui")


@v2_router.patch("/memories/{memory_id}", response_model=MemoryResponse, tags=["memories"])
def update_memory(memory_id: UUID, payload: MemoryWrite, services: ServiceDependency, user_id: UserDependency):
    return services.memories.update(user_id, memory_id, payload.content)


@v2_router.post("/memories/{memory_id}/retry", response_model=MemoryResponse, tags=["memories"])
def retry_memory(memory_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return services.memories.retry(user_id, memory_id)


@v2_router.delete("/memories/{memory_id}", status_code=204, tags=["memories"])
def delete_memory(memory_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.memories.delete(user_id, memory_id)
    return Response(status_code=204)


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\nid: {data['run_id']}:{data['sequence']}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def document_response(document, embedding_model: str) -> dict:
    if document.status == "deleting":
        reason = "DOCUMENT_DELETING"
    elif document.status != "ready":
        reason = "DOCUMENT_NOT_READY"
    elif document.embedding_model != embedding_model:
        reason = "EMBEDDING_MODEL_MISMATCH"
    else:
        reason = None
    return {
        "id": document.id, "original_name": document.original_name, "file_type": document.file_type,
        "status": document.status, "chunk_count": document.chunk_count, "embedding_model": document.embedding_model,
        "error_message": document.error_message, "size_bytes": document.size_bytes,
        "can_query": reason is None, "unavailable_reason": reason,
        "version_info": getattr(document, "version_info", None),
        "created_at": document.created_at, "updated_at": document.updated_at,
    }


@health_router.get("/health/live")
def live():
    return {"status": "alive"}


@health_router.get("/health/ready")
def ready(services: ServiceDependency):
    result = services.readiness()
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


@v2_router.post("/documents", response_model=DocumentResponse, status_code=202, tags=["documents"])
def upload_document(services: ServiceDependency, user_id: UserDependency, file: Annotated[UploadFile, File()]):
    try:
        document = services.ingestion.upload(user_id, file.filename or "", file.content_type, file.file)
    finally:
        file.file.close()
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.get("/documents/{document_id}", response_model=DocumentResponse, tags=["documents"])
def get_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return document_response(services.ingestion.get(user_id, document_id), services.settings.embedding_model)


@v2_router.get("/documents/{document_id}/versions", response_model=DocumentPage, tags=["documents"])
def document_versions(document_id: UUID, services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50):
    documents, total = services.ingestion.versions(user_id, document_id, offset, limit)
    return {"items": [document_response(document, services.settings.embedding_model) for document in documents],
            "total": total, "offset": offset, "limit": limit}


@v2_router.post("/documents/{document_id}/versions", response_model=DocumentResponse, status_code=202, tags=["documents"])
def upload_document_version(document_id: UUID, services: ServiceDependency, user_id: UserDependency, file: Annotated[UploadFile, File()]):
    try:
        document = services.ingestion.upload(user_id, file.filename or "", file.content_type, file.file, base_document_id=document_id)
    finally:
        file.file.close()
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.post("/documents/{document_id}/activate", response_model=DocumentResponse, tags=["documents"])
def activate_document_version(document_id: UUID, payload: ActivateDocumentVersion, services: ServiceDependency, user_id: UserDependency):
    document = services.ingestion.activate_version(user_id, document_id, payload.expected_current_document_id)
    return document_response(document, services.settings.embedding_model)


@v2_router.post("/documents/{document_id}/retry", response_model=DocumentResponse, status_code=202, tags=["documents"])
def retry_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    document = services.ingestion.retry(user_id, document_id)
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.delete("/documents/{document_id}", status_code=204, tags=["documents"])
def delete_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.ingestion.delete(user_id, document_id)
    return Response(status_code=204)


@v2_router.post("/sessions", response_model=SessionResponse, status_code=201, tags=["v2 sessions"])
def create_v2_session(payload: SessionCreate, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.create(user_id, payload.title)


@v2_router.get("/sessions", response_model=list[SessionResponse], tags=["v2 sessions"])
def list_v2_sessions(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50, q: Search = ""):
    return services.sessions.list(user_id, offset, limit, query=q.strip())


@v2_router.get("/sessions/{session_id}", response_model=V2SessionDetail, tags=["v2 sessions"])
def get_v2_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency, message_limit: Limit = 100, message_offset: Offset = 0):
    session, messages = services.sessions.get(user_id, session_id, message_limit, message_offset)
    return V2SessionDetail(
        **SessionResponse.model_validate(session).model_dump(),
        messages=[V2MessageResponse.model_validate(message) for message in messages],
    )


@v2_router.delete("/sessions/{session_id}", status_code=204, tags=["v2 sessions"])
def delete_v2_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.sessions.delete(user_id, session_id)
    return Response(status_code=204)


@v2_router.patch("/sessions/{session_id}", response_model=SessionResponse, tags=["v2 sessions"])
def rename_v2_session(session_id: UUID, payload: SessionRename, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.rename(user_id, session_id, payload.title)


@v2_router.post("/sessions/{session_id}/messages", response_model=V2ChatResponse, tags=["v2 chat"])
def chat_v2(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency):
    data = AgentChatInput(
        question=payload.question, model_provider=payload.model_provider, search_mode=payload.search_mode,
        document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
        web_query=payload.web_query, web_urls=tuple(payload.web_urls), knowledge_base_ids=tuple(payload.knowledge_base_ids),
    )
    message = services.chat.answer_v2(user_id, session_id, data, request.state.request_id)
    return V2ChatResponse(
        message_id=message.id, answer=message.content, model_provider=message.model_provider,
        model_name=message.model_name, search_mode=message.run_metadata["search_mode"],
        citations=message.citations, token_usage=message.token_usage, run_metadata=message.run_metadata,
    )


@v2_router.post("/sessions/{session_id}/messages/stream", tags=["v2 chat"])
def stream_chat_v2(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency):
    data = AgentChatInput(
        question=payload.question, model_provider=payload.model_provider, search_mode=payload.search_mode,
        document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
        web_query=payload.web_query, web_urls=tuple(payload.web_urls), knowledge_base_ids=tuple(payload.knowledge_base_ids),
    )
    if services.settings.chat_runs_enabled:
        client_key = request.headers.get("Idempotency-Key")
        try:
            key = UUID(client_key) if client_key else uuid4()
        except ValueError:
            raise AppError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 必须为 UUID", 422) from None
        run = services.runs.start(user_id, session_id, data, request.state.request_id, key)
        return durable_stream(run.id, request, services, user_id, 0)
    run_id = str(uuid4())
    request_id = request.state.request_id

    def events():
        sequence = 1
        yield sse_event("run_started", {
            "run_id": run_id, "sequence": sequence, "request_id": request_id,
            "search_mode": payload.search_mode,
        })
        pending = Queue()

        def answer():
            try:
                message = services.chat.answer_v2(
                    user_id, session_id, data, request_id,
                    on_answer_event=lambda event, value: pending.put((event, value)),
                )
                pending.put(("result", message))
            except Exception as error:
                pending.put(("failure", error))

        Thread(target=answer, daemon=True).start()
        try:
            while True:
                try:
                    event, value = pending.get(timeout=15)
                except Empty:
                    yield ": keep-alive\n\n"
                    continue
                if event == "failure":
                    raise value
                if event == "result":
                    message = value
                    break
                sequence += 1
                event_data = {"run_id": run_id, "sequence": sequence}
                if event == "answer_delta":
                    event_data["delta"] = value
                elif event in {"agent_started", "agent_finished", "agent_failed"}:
                    event_data.update(value)
                yield sse_event(event, event_data)
            metadata = message.run_metadata or {}
            metrics = metadata.get("metrics", {})
            context = metrics.get("context")
            if context:
                sequence += 1
                yield sse_event("context_usage", {"run_id": run_id, "sequence": sequence, "context": context})
            sequence += 1
            yield sse_event("usage", {
                "run_id": run_id, "sequence": sequence, "token_usage": message.token_usage,
                "cache": metrics.get("cache"),
            })
            sequence += 1
            yield sse_event("done", {
                "run_id": run_id, "sequence": sequence, "message_id": message.id,
                "answer": message.content, "model_provider": message.model_provider,
                "model_name": message.model_name,
                "search_mode": metadata.get("search_mode", payload.search_mode),
                "citations": message.citations, "token_usage": message.token_usage,
                "run_metadata": metadata,
            })
        except AppError as error:
            sequence += 1
            yield sse_event("error", {
                "run_id": run_id, "sequence": sequence, "code": error.code,
                "message": error.message, "status": error.status_code, "request_id": request_id,
            })
        except Exception:
            logger.exception("stream_chat_failed", extra={"request_id": request_id, "resource_id": str(session_id)})
            sequence += 1
            yield sse_event("error", {
                "run_id": run_id, "sequence": sequence, "code": "INTERNAL_ERROR",
                "message": "服务内部错误，请根据 request_id 检查日志", "request_id": request_id,
            })

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def require_runs(services):
    if not services.settings.chat_runs_enabled:
        raise AppError("CHAT_RUNS_DISABLED", "问答事件持久化尚未启用", 503)


def run_response(run):
    return {"run_id": run.id, "session_id": run.session_id, "state": run.state,
            "sequence": run.sequence, "message_id": run.message_id, "expires_at": run.expires_at,
            "events_url": f"/api/v2/runs/{run.id}/events"}


@v2_router.post("/sessions/{session_id}/runs", status_code=202, tags=["v2 runs"])
def start_run(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency,
              client_key: Annotated[UUID, Header(alias="Idempotency-Key")]):
    require_runs(services)
    data = AgentChatInput(question=payload.question, model_provider=payload.model_provider, search_mode=payload.search_mode,
                          document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
                          web_query=payload.web_query, web_urls=tuple(payload.web_urls), knowledge_base_ids=tuple(payload.knowledge_base_ids))
    return run_response(services.runs.start(user_id, session_id, data, request.state.request_id, client_key))


@v2_router.get("/sessions/{session_id}/runs", tags=["v2 runs"])
def session_runs(session_id: UUID, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    with services.database.sessions() as database:
        Repository(database).chat_session(user_id, session_id)
        ids = list(database.scalars(select(ChatRun.id).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id)
                                   .order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).limit(20)))
    return [run_response(services.runs.store.read(user_id, run_id)[0]) for run_id in ids]


@v2_router.get("/runs/{run_id}", tags=["v2 runs"])
def get_run(run_id: UUID, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    return run_response(services.runs.store.read(user_id, run_id)[0])


def durable_stream(run_id, request, services, user_id, after):
    header = request.headers.get("Last-Event-ID")
    if header:
        try:
            header_run, raw_sequence = header.rsplit(":", 1)
            if UUID(header_run) != run_id or not raw_sequence.isdecimal():
                raise ValueError()
            after = int(raw_sequence)
            if after > 2**31 - 1:
                raise ValueError()
        except ValueError:
            raise AppError("INVALID_EVENT_CURSOR", "Last-Event-ID 与本轮问答不匹配", 422) from None
    initial = services.runs.store.read(user_id, run_id, after)

    async def events():
        cursor, batch = after, initial
        while not await request.is_disconnected():
            run, rows = batch
            for row in rows:
                cursor = row.sequence
                yield sse_event(row.event, row.payload)
            if run.state != "running" and cursor >= run.sequence:
                return
            if not rows:
                yield ": keep-alive\n\n"
                await asyncio.sleep(.5)
            batch = await run_in_threadpool(services.runs.store.read, user_id, run_id, cursor)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no", "X-Run-ID": str(run_id)})


@v2_router.get("/runs/{run_id}/events", tags=["v2 runs"])
def replay_run(run_id: UUID, request: Request, services: ServiceDependency, user_id: UserDependency,
               after: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0):
    require_runs(services)
    return durable_stream(run_id, request, services, user_id, after)


@v2_router.get("/documents/{document_id}/processing", tags=["v2 documents"])
def processing_v2(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    document = services.ingestion.get(user_id, document_id)
    metadata = document.parser_metadata or {}
    return {
        "document_id": document.id, "status": document.status,
        "parser": metadata.get("provider"), "document_type": metadata.get("document_type"),
        "phase": metadata.get("phase"), "parsed_unit_count": metadata.get("parsed_unit_count"),
        "unit_kind": metadata.get("unit_kind"), "error_code": metadata.get("error_code"),
        "task": services.ingestion.queue.status(user_id, document_id) if getattr(services.ingestion, "queue", None) else None,
    }


@v2_router.get("/documents", response_model=DocumentPage, tags=["v2 documents"])
def search_v2_documents(services: ServiceDependency, user_id: UserDependency, q: Search = "", status: Annotated[str | None, Query(pattern="^(processing|ready|failed|deleting)$")] = None, offset: Offset = 0, limit: Limit = 50):
    items, total = services.ingestion.search(user_id, q.strip(), status, offset, limit)
    return {"items": [document_response(item, services.settings.embedding_model) for item in items], "total": total, "offset": offset, "limit": limit}


@v2_router.get("/capabilities", tags=["v2 capabilities"])
def capabilities(services: ServiceDependency, user_id: UserDependency):
    snapshot = services.model_center.snapshot() if services.settings.model_center_enabled else None
    settings = snapshot.settings if snapshot else services.settings
    office = [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls"] if settings.mineru_enabled and settings.mineru_api_token.get_secret_value() else []
    models = [
        {"provider": "deepseek", "model_name": settings.deepseek_model, "available": bool(settings.deepseek_api_key.get_secret_value()) and "deepseek" in settings.enabled_model_providers, "input_budget_tokens": min(settings.agent_context_token_budget, settings.deepseek_context_window - settings.model_max_tokens), "cache_usage_supported": True},
        {"provider": "ollama", "model_name": settings.ollama_model, "available": "ollama" in settings.enabled_model_providers, "input_budget_tokens": min(settings.agent_context_token_budget, settings.ollama_context_window - settings.model_max_tokens), "cache_usage_supported": False},
    ]
    if snapshot:
        models = services.model_center.profiles(snapshot)
    available = {item["provider"] for item in models if item["available"]}
    return {"schema_version": 1, "default_model_provider": settings.default_model_provider if settings.default_model_provider in available else None, "models": models, "web_search_enabled": settings.web_search_enabled, "web_read_enabled": True, "sql_enabled": bool(settings.sql_database_url.get_secret_value() and settings.sql_allowed_tables), "upload": {"allowed_extensions": office + [".txt", ".md", ".xlsx"], "max_file_bytes": settings.max_upload_mb * 1024 * 1024, "long_running_warning_seconds": 900}, "features": {"model_center": settings.model_center_enabled, "sharing": settings.sharing_enabled, "durable_runs": settings.chat_runs_enabled, "document_versions": settings.document_versions_enabled, "parent_context": settings.retrieval_parent_context, "multi_agent": True, "authentication": True, "long_term_memory": True, "document_search": True, "document_queryability": True, "session_search": True, "session_rename": True, "metrics": True, "context_estimate": True, "context_compaction": True, "streaming": True}}


@v2_router.post("/context-estimate", tags=["v2 context"])
def context_estimate(payload: ContextEstimateRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.estimate_context(user_id, payload)


@v2_router.post("/sessions/{session_id}/compact", tags=["v2 context"])
def compact_context(session_id: UUID, payload: CompactRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.compact(user_id, session_id, payload.expected_context_version, payload.model_provider)
