import json
import logging
import asyncio
from queue import Empty, Queue
from threading import Thread
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Cookie, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.encoders import jsonable_encoder
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.dependencies import AdminDependency, AuthDependency, ServiceDependency, UserDependency
from app.api.schemas import (
    AuthResponse, CredentialsRequest, RegisterRequest, DocumentResponse, MemoryResponse, MemoryWrite, SessionCreate, SessionResponse, SessionRename, UserResponse,
    V2ChatRequest, V2ChatResponse, V2MessageResponse, V2SessionDetail,
    CompactRequest, ContextEstimateRequest, DocumentPage, ActivateDocumentVersion, RetireDocumentSeries, RunResumeRequest, DocumentTaskPage, DocumentTaskResponse,
)
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput
from app.application.runs import TERMINAL_STATES
from app.infrastructure.persistence.models import ChatMessage, ChatRun
from app.infrastructure.persistence.repository import Repository

v2_router = APIRouter(prefix="/api/v2")
health_router = APIRouter(tags=["health"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]
logger = logging.getLogger("rag")
REFRESH_COOKIE = "rag_refresh"


def workspace_options(organization_id):
    return {"organization_id": organization_id} if organization_id is not None else {}


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
        "evaluation": settings.evaluation_enabled,
        "capability_center": settings.capability_center_enabled, "tool_approvals": settings.tool_approvals_enabled,
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
    elif document.embedding_model != embedding_model and not getattr(document, "index_bound_retrieval", False):
        reason = "EMBEDDING_MODEL_MISMATCH"
    elif not getattr(document, "index_queryable", True):
        reason = "DOCUMENT_INDEX_UNPUBLISHED"
    else:
        reason = None
    return {
        "id": document.id, "original_name": document.original_name, "file_type": document.file_type,
        "organization_id": getattr(document, "organization_id", None),
        "status": document.status, "chunk_count": document.chunk_count, "embedding_model": document.embedding_model,
        "error_message": document.error_message, "size_bytes": document.size_bytes,
        "can_query": reason is None, "unavailable_reason": reason,
        "version_info": getattr(document, "version_info", None),
        "index_info": getattr(document, "index_info", None),
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
def upload_document(services: ServiceDependency, user_id: UserDependency, file: Annotated[UploadFile, File()], organization_id: UUID | None = None):
    try:
        document = services.ingestion.upload(user_id, file.filename or "", file.content_type, file.file, **workspace_options(organization_id))
    finally:
        file.file.close()
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.get("/documents/{document_id}", response_model=DocumentResponse, tags=["documents"])
def get_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    return document_response(services.ingestion.get(user_id, document_id, **workspace_options(organization_id)), services.settings.embedding_model)


@v2_router.get("/documents/{document_id}/versions", response_model=DocumentPage, tags=["documents"])
def document_versions(document_id: UUID, services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50, organization_id: UUID | None = None):
    documents, total = services.ingestion.versions(user_id, document_id, offset, limit, **workspace_options(organization_id))
    return {"items": [document_response(document, services.settings.embedding_model) for document in documents],
            "total": total, "offset": offset, "limit": limit}


@v2_router.post("/documents/{document_id}/versions", response_model=DocumentResponse, status_code=202, tags=["documents"])
def upload_document_version(document_id: UUID, services: ServiceDependency, user_id: UserDependency, file: Annotated[UploadFile, File()], organization_id: UUID | None = None):
    try:
        document = services.ingestion.upload(user_id, file.filename or "", file.content_type, file.file, base_document_id=document_id, **workspace_options(organization_id))
    finally:
        file.file.close()
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.post("/documents/{document_id}/activate", response_model=DocumentResponse, tags=["documents"])
def activate_document_version(document_id: UUID, payload: ActivateDocumentVersion, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    document = services.ingestion.activate_version(user_id, document_id, payload.expected_current_document_id, **workspace_options(organization_id))
    return document_response(document, services.settings.embedding_model)


@v2_router.post("/documents/{document_id}/retry", response_model=DocumentResponse, status_code=202, tags=["documents"])
def retry_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    document = services.ingestion.retry(user_id, document_id, **workspace_options(organization_id))
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@v2_router.delete("/documents/{document_id}", status_code=204,
                  responses={202: {"model": DocumentTaskResponse, "description": "版本退休清理任务已入队"}}, tags=["documents"])
def delete_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    task = services.ingestion.delete(user_id, document_id, **workspace_options(organization_id))
    if task is not None:
        from app.application.document_tasks import task_response
        return JSONResponse(jsonable_encoder(task_response(task)), status_code=202)
    return Response(status_code=204)


@v2_router.post("/documents/{document_id}/retire-series", response_model=DocumentTaskResponse, status_code=202, tags=["documents"])
def retire_document_series(document_id: UUID, body: RetireDocumentSeries, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    from app.application.document_tasks import task_response
    return task_response(services.ingestion.tasks.retire_series(user_id, document_id, expected_version_count=body.expected_version_count, **workspace_options(organization_id)))


@v2_router.get("/document-tasks", response_model=DocumentTaskPage, tags=["documents"])
def document_tasks(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50,
                   organization_id: UUID | None = None, q: Search = "", parent_task_id: UUID | None = None,
                   state: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None,
                   kind: Literal["ingest", "hierarchy", "delete", "delete_series"] | None = None):
    return services.ingestion.tasks.list(user_id, offset, limit, task_state=state, kind=kind, search=q.strip(), parent_task_id=parent_task_id, **workspace_options(organization_id))


@v2_router.get("/document-tasks/{task_id}", response_model=DocumentTaskResponse, tags=["documents"])
def document_task(task_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    return services.ingestion.tasks.detail(user_id, task_id, **workspace_options(organization_id))


@v2_router.post("/document-tasks/{task_id}/retry", response_model=DocumentTaskResponse, status_code=202, tags=["documents"])
def retry_document_task(task_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    from app.application.document_tasks import task_response
    return task_response(services.ingestion.tasks.retry(user_id, task_id, **workspace_options(organization_id)))


@v2_router.post("/sessions", response_model=SessionResponse, status_code=201, tags=["v2 sessions"])
def create_v2_session(payload: SessionCreate, services: ServiceDependency, user_id: UserDependency):
    if payload.organization_id is not None:
        return services.sessions.create(user_id, payload.title, organization_id=payload.organization_id)
    return services.sessions.create(user_id, payload.title)


@v2_router.get("/sessions", response_model=list[SessionResponse], tags=["v2 sessions"])
def list_v2_sessions(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50, q: Search = "", organization_id: UUID | None = None):
    if organization_id is not None:
        return services.sessions.list(user_id, offset, limit, query=q.strip(), organization_id=organization_id)
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
        question=payload.question, model_provider=payload.model_provider, model_profile_id=payload.model_profile_id, skill_name=payload.skill_name, approved_tools=tuple(payload.approved_tools), approval_catalog_revision=payload.approval_catalog_revision, search_mode=payload.search_mode,
        document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
        web_query=payload.web_query, web_urls=tuple(payload.web_urls), knowledge_base_ids=tuple(payload.knowledge_base_ids),
    )
    if services.settings.chat_runs_enabled:
        try:
            key = UUID(request.headers["Idempotency-Key"]) if "Idempotency-Key" in request.headers else uuid4()
        except ValueError:
            raise AppError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 必须为 UUID", 422) from None
        run = services.runs.start(user_id, session_id, data, request.state.request_id, key)
        run, events = services.runs.wait(user_id, run.id)
        if run.state not in TERMINAL_STATES and run.state != "waiting":
            return JSONResponse(jsonable_encoder(run_response(run)), status_code=202, headers={"X-Run-ID": str(run.id)})
        if run.state not in {"waiting", "succeeded"}:
            error = next((event.payload for event in reversed(events) if event.event == "error"), {})
            raise AppError(error.get("code", "RUN_CANCELLED"), error.get("message", "本轮问答已取消"), error.get("status", 409))
        with services.database.sessions() as database:
            message = database.get(ChatMessage, run.message_id)
    else:
        message = services.chat.answer_v2(user_id, session_id, data, request.state.request_id)
    return V2ChatResponse(
        message_id=message.id, answer=message.content, model_provider=message.model_provider,
        model_name=message.model_name, search_mode=message.run_metadata["search_mode"],
        citations=message.citations, token_usage=message.token_usage, run_metadata=message.run_metadata,
    )


@v2_router.post("/sessions/{session_id}/messages/stream", tags=["v2 chat"])
def stream_chat_v2(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency):
    data = AgentChatInput(
        question=payload.question, model_provider=payload.model_provider, model_profile_id=payload.model_profile_id, skill_name=payload.skill_name, approved_tools=tuple(payload.approved_tools), approval_catalog_revision=payload.approval_catalog_revision, search_mode=payload.search_mode,
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
            "waiting": getattr(run, "waiting", None),
            "events_url": f"/api/v2/runs/{run.id}/events"}


@v2_router.post("/sessions/{session_id}/runs", status_code=202, tags=["v2 runs"])
def start_run(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency,
              client_key: Annotated[UUID, Header(alias="Idempotency-Key")]):
    require_runs(services)
    data = AgentChatInput(question=payload.question, model_provider=payload.model_provider, model_profile_id=payload.model_profile_id, skill_name=payload.skill_name, approved_tools=tuple(payload.approved_tools), approval_catalog_revision=payload.approval_catalog_revision, search_mode=payload.search_mode,
                          document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
                          web_query=payload.web_query, web_urls=tuple(payload.web_urls), knowledge_base_ids=tuple(payload.knowledge_base_ids))
    run = services.runs.start(user_id, session_id, data, request.state.request_id, client_key)
    return run_response(services.runs.store.read(user_id, run.id)[0])


@v2_router.get("/sessions/{session_id}/runs", tags=["v2 runs"])
def session_runs(session_id: UUID, response: Response, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    response.headers["Cache-Control"] = "no-store"
    with services.database.sessions() as database:
        Repository(database).chat_session(user_id, session_id, kind="chat")
        ids = list(database.scalars(select(ChatRun.id).where(ChatRun.session_id == session_id, ChatRun.user_id == user_id)
                                   .order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).limit(20)))
    return [run_response(services.runs.store.read(user_id, run_id)[0]) for run_id in ids]


@v2_router.get("/runs/{run_id}", tags=["v2 runs"])
def get_run(run_id: UUID, response: Response, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    response.headers["Cache-Control"] = "no-store"
    return run_response(services.runs.store.read(user_id, run_id)[0])


@v2_router.post("/runs/{run_id}/cancel", tags=["v2 runs"])
def cancel_run(run_id: UUID, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    services.runs.store.request_cancel(user_id, run_id)
    return run_response(services.runs.store.read(user_id, run_id)[0])


@v2_router.post("/runs/{run_id}/resume", status_code=202, tags=["v2 runs"])
def resume_run(run_id: UUID, payload: RunResumeRequest, services: ServiceDependency, user_id: UserDependency):
    require_runs(services)
    options = {"public_query": payload.public_query} if payload.public_query is not None else {}
    services.runs.store.resume(user_id, run_id, payload.checkpoint_revision, payload.resume_token, payload.supplement, **options)
    return run_response(services.runs.store.read(user_id, run_id)[0])


@v2_router.get('/runs/{run_id}/trace', tags=['v2 runs'])
def run_trace(run_id: UUID, response: Response, services: ServiceDependency, user_id: UserDependency,
              after: Annotated[int, Query(ge=0,le=2**31-1)] = 0):
    require_runs(services)
    from app.application.run_trace import read_trace
    response.headers.update({'Cache-Control':'no-store','Pragma':'no-cache'})
    return read_trace(services.runs.store,user_id,run_id,after)


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
        async with services.runs.notifications.subscribe(run_id) as signal:
            cursor, batch = after, initial
            while not await request.is_disconnected():
                signal.clear()
                run, rows = batch
                for row in rows:
                    cursor = row.sequence
                    data = row.payload
                    if row.event == "run_waiting":
                        data = {**data, "is_current": run.state == "waiting" and row.sequence == run.sequence}
                    yield sse_event(row.event, data)
                if (run.state in TERMINAL_STATES or run.state == "waiting") and cursor >= run.sequence:
                    return
                if not rows:
                    yield ": keep-alive\n\n"
                    try:
                        await asyncio.wait_for(signal.wait(), timeout=.5)
                    except TimeoutError:
                        pass  # Polling remains authoritative when notifications are lost.
                batch = await run_in_threadpool(services.runs.store.read, user_id, run_id, cursor)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no", "X-Run-ID": str(run_id)})


@v2_router.get("/runs/{run_id}/events", tags=["v2 runs"])
def replay_run(run_id: UUID, request: Request, services: ServiceDependency, user_id: UserDependency,
               after: Annotated[int, Query(ge=0, le=2**31 - 1)] = 0):
    require_runs(services)
    return durable_stream(run_id, request, services, user_id, after)


@v2_router.get("/documents/{document_id}/processing", tags=["v2 documents"])
def processing_v2(document_id: UUID, services: ServiceDependency, user_id: UserDependency, organization_id: UUID | None = None):
    document = services.ingestion.get(user_id, document_id, **workspace_options(organization_id))
    metadata = document.parser_metadata or {}
    return {
        "document_id": document.id, "status": document.status,
        "parser": metadata.get("provider"), "document_type": metadata.get("document_type"),
        "phase": metadata.get("phase"), "parsed_unit_count": metadata.get("parsed_unit_count"),
        "unit_kind": metadata.get("unit_kind"), "error_code": metadata.get("error_code"),
        "task": services.ingestion.queue.status(user_id, document_id) if getattr(services.ingestion, "queue", None) else None,
    }


@v2_router.get("/documents", response_model=DocumentPage, tags=["v2 documents"])
def search_v2_documents(services: ServiceDependency, user_id: UserDependency, q: Search = "", status: Annotated[str | None, Query(pattern="^(processing|ready|failed|deleting)$")] = None, offset: Offset = 0, limit: Limit = 50, organization_id: UUID | None = None):
    items, total = services.ingestion.search(user_id, q.strip(), status, offset, limit, **workspace_options(organization_id))
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
    default_profile_id = None
    if snapshot:
        models, default_profile_id = services.model_center.selectable_profiles(snapshot)
    available = {item["provider"] for item in models if item["available"]}
    if default_profile_id not in {item.get("profile_id") for item in models if item["available"]}:
        default_profile_id = None
    web_read_enabled = True
    if settings.capability_center_enabled:
        _, catalog = services.capabilities.snapshot()
        web_read_enabled = catalog.web is None or catalog.web.declaration.enabled
        settings = services.capabilities.project_sql(settings, services.capabilities.sql_policy(catalog))
        if not web_read_enabled:
            settings = settings.model_copy(update={"web_search_enabled": False})
    return {"schema_version": 1, "default_model_provider": settings.default_model_provider if settings.default_model_provider in available else None, "default_model_profile_id": default_profile_id, "models": models, "web_search_enabled": settings.web_search_enabled, "web_read_enabled": web_read_enabled, "sql_enabled": bool(settings.sql_database_url.get_secret_value() and settings.sql_allowed_tables), "upload": {"allowed_extensions": office + [".txt", ".md", ".html", ".htm", ".xlsx"], "max_file_bytes": settings.max_upload_mb * 1024 * 1024, "long_running_warning_seconds": 900}, "features": {"capability_center": settings.capability_center_enabled, "tool_approvals": settings.tool_approvals_enabled, "document_tasks": settings.ingestion_mode == "durable", "document_publication": settings.document_publication_enabled, "workspaces": settings.workspaces_enabled, "model_center": settings.model_center_enabled, "model_profiles": settings.model_center_enabled, "evaluation": settings.evaluation_enabled, "sharing": settings.sharing_enabled, "durable_runs": settings.chat_runs_enabled, "document_versions": settings.document_versions_enabled, "parent_context": settings.retrieval_parent_context, "multi_agent": True, "authentication": True, "long_term_memory": True, "document_search": True, "document_queryability": True, "session_search": True, "session_rename": True, "metrics": True, "context_estimate": True, "context_compaction": True, "streaming": True}}


@v2_router.post("/context-estimate", tags=["v2 context"])
def context_estimate(payload: ContextEstimateRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.estimate_context(user_id, payload)


@v2_router.post("/sessions/{session_id}/compact", tags=["v2 context"])
def compact_context(session_id: UUID, payload: CompactRequest, services: ServiceDependency, user_id: UserDependency):
    if payload.model_profile_id is not None:
        return services.chat.compact(user_id, session_id, payload.expected_context_version, payload.model_provider, profile_id=payload.model_profile_id)
    return services.chat.compact(user_id, session_id, payload.expected_context_version, payload.model_provider)
