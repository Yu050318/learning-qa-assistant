from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from app.api.dependencies import ServiceDependency, UserDependency
from app.api.schemas import (
    ChatRequest, ChatResponse, DocumentResponse, MessageResponse, SessionCreate, SessionDetail,
    SessionResponse, SessionRename, V2ChatRequest, V2ChatResponse, V2MessageResponse, V2SessionDetail,
    CompactRequest, ContextEstimateRequest, DocumentPage,
)
from app.core.errors import AppError
from app.domain.contracts import AgentChatInput

router = APIRouter(prefix="/api/v1")
v2_router = APIRouter(prefix="/api/v2")
health_router = APIRouter(tags=["health"])
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=200)]


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
        "created_at": document.created_at, "updated_at": document.updated_at,
    }


@health_router.get("/health/live")
def live():
    return {"status": "alive"}


@health_router.get("/health/ready")
def ready(services: ServiceDependency):
    result = services.readiness()
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


@router.post("/documents", response_model=DocumentResponse, status_code=202, tags=["documents"])
def upload_document(services: ServiceDependency, user_id: UserDependency, file: Annotated[UploadFile, File()]):
    try:
        document = services.ingestion.upload(user_id, file.filename or "", file.content_type, file.file)
    finally:
        file.file.close()
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@router.get("/documents", response_model=list[DocumentResponse], tags=["documents"])
def list_documents(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50):
    return [document_response(item, services.settings.embedding_model) for item in services.ingestion.list(user_id, offset, limit)]


@router.get("/documents/{document_id}", response_model=DocumentResponse, tags=["documents"])
def get_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    return document_response(services.ingestion.get(user_id, document_id), services.settings.embedding_model)


@router.post("/documents/{document_id}/retry", response_model=DocumentResponse, status_code=202, tags=["documents"])
def retry_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    document = services.ingestion.retry(user_id, document_id)
    services.ingestion.schedule(user_id, document.id)
    return document_response(document, services.settings.embedding_model)


@router.delete("/documents/{document_id}", status_code=204, tags=["documents"])
def delete_document(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.ingestion.delete(user_id, document_id)
    return Response(status_code=204)


@v2_router.post("/sessions", response_model=SessionResponse, status_code=201, tags=["v2 sessions"])
def create_v2_session(payload: SessionCreate, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.create(user_id, payload.title, api_version="v2")


@v2_router.get("/sessions", response_model=list[SessionResponse], tags=["v2 sessions"])
def list_v2_sessions(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50, q: Search = ""):
    return services.sessions.list(user_id, offset, limit, api_version="v2", query=q.strip())


@v2_router.get("/sessions/{session_id}", response_model=V2SessionDetail, tags=["v2 sessions"])
def get_v2_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency, message_limit: Limit = 100, message_offset: Offset = 0):
    session, messages = services.sessions.get(user_id, session_id, message_limit, message_offset, api_version="v2")
    return V2SessionDetail(
        **SessionResponse.model_validate(session).model_dump(),
        messages=[V2MessageResponse.model_validate(message) for message in messages],
    )


@v2_router.delete("/sessions/{session_id}", status_code=204, tags=["v2 sessions"])
def delete_v2_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.sessions.delete(user_id, session_id, api_version="v2")
    return Response(status_code=204)


@v2_router.patch("/sessions/{session_id}", response_model=SessionResponse, tags=["v2 sessions"])
def rename_v2_session(session_id: UUID, payload: SessionRename, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.rename(user_id, session_id, payload.title)


@v2_router.post("/sessions/{session_id}/messages", response_model=V2ChatResponse, tags=["v2 chat"])
def chat_v2(session_id: UUID, payload: V2ChatRequest, request: Request, services: ServiceDependency, user_id: UserDependency):
    data = AgentChatInput(
        question=payload.question, model_provider=payload.model_provider, search_mode=payload.search_mode,
        document_ids=tuple(payload.document_ids) if payload.document_ids is not None else None,
        web_query=payload.web_query,
    )
    message = services.chat.answer_v2(user_id, session_id, data, request.state.request_id)
    return V2ChatResponse(
        message_id=message.id, answer=message.content, model_provider=message.model_provider,
        model_name=message.model_name, search_mode=message.run_metadata["search_mode"],
        citations=message.citations, token_usage=message.token_usage, run_metadata=message.run_metadata,
    )


@v2_router.get("/documents/{document_id}/processing", tags=["v2 documents"])
def processing_v2(document_id: UUID, services: ServiceDependency, user_id: UserDependency):
    document = services.ingestion.get(user_id, document_id)
    metadata = document.parser_metadata or {}
    return {
        "document_id": document.id, "status": document.status,
        "parser": metadata.get("provider"), "document_type": metadata.get("document_type"),
        "phase": metadata.get("phase"), "parsed_unit_count": metadata.get("parsed_unit_count"),
        "unit_kind": metadata.get("unit_kind"), "error_code": metadata.get("error_code"),
    }


@v2_router.get("/documents", response_model=DocumentPage, tags=["v2 documents"])
def search_v2_documents(services: ServiceDependency, user_id: UserDependency, q: Search = "", status: Annotated[str | None, Query(pattern="^(processing|ready|failed|deleting)$")] = None, offset: Offset = 0, limit: Limit = 50):
    items, total = services.ingestion.search(user_id, q.strip(), status, offset, limit)
    return {"items": [document_response(item, services.settings.embedding_model) for item in items], "total": total, "offset": offset, "limit": limit}


@v2_router.get("/capabilities", tags=["v2 capabilities"])
def capabilities(services: ServiceDependency, user_id: UserDependency):
    settings = services.settings
    office = [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"] if settings.mineru_enabled and settings.mineru_api_token.get_secret_value() else []
    models = [
        {"provider": "deepseek", "model_name": settings.deepseek_model, "available": bool(settings.deepseek_api_key.get_secret_value()), "input_budget_tokens": min(settings.agent_context_token_budget, settings.deepseek_context_window - settings.model_max_tokens), "cache_usage_supported": True},
        {"provider": "ollama", "model_name": settings.ollama_model, "available": True, "input_budget_tokens": min(settings.agent_context_token_budget, settings.ollama_context_window - settings.model_max_tokens), "cache_usage_supported": False},
    ]
    available = {item["provider"] for item in models if item["available"]}
    return {"schema_version": 1, "default_model_provider": settings.default_model_provider if settings.default_model_provider in available else None, "models": models, "web_search_enabled": settings.web_search_enabled, "upload": {"allowed_extensions": office + [".txt", ".md"], "max_file_bytes": settings.max_upload_mb * 1024 * 1024, "long_running_warning_seconds": 900}, "features": {"document_search": True, "document_queryability": True, "session_search": True, "session_rename": True, "metrics": True, "context_estimate": True, "context_compaction": True, "streaming": False}}


@v2_router.post("/context-estimate", tags=["v2 context"])
def context_estimate(payload: ContextEstimateRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.estimate_context(user_id, payload)


@v2_router.post("/sessions/{session_id}/compact", tags=["v2 context"])
def compact_context(session_id: UUID, payload: CompactRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.compact(user_id, session_id, payload.expected_context_version, payload.model_provider)


@router.post("/sessions", response_model=SessionResponse, status_code=201, tags=["sessions"])
def create_session(payload: SessionCreate, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.create(user_id, payload.title)


@router.get("/sessions", response_model=list[SessionResponse], tags=["sessions"])
def list_sessions(services: ServiceDependency, user_id: UserDependency, offset: Offset = 0, limit: Limit = 50):
    return services.sessions.list(user_id, offset, limit)


@router.get("/sessions/{session_id}", response_model=SessionDetail, tags=["sessions"])
def get_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency, message_limit: Limit = 100, message_offset: Offset = 0):
    session, messages = services.sessions.get(user_id, session_id, message_limit, message_offset)
    return SessionDetail(
        **SessionResponse.model_validate(session).model_dump(),
        messages=[MessageResponse.model_validate(message) for message in messages],
    )


@router.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
def delete_session(session_id: UUID, services: ServiceDependency, user_id: UserDependency):
    services.sessions.delete(user_id, session_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/messages", response_model=ChatResponse, tags=["chat"])
def chat(session_id: UUID, payload: ChatRequest, services: ServiceDependency, user_id: UserDependency):
    return services.chat.answer(user_id, session_id, payload.question, payload.document_ids, payload.model_provider)


@router.post("/sessions/{session_id}/messages/stream", tags=["chat"], responses={501: {"description": "SSE 尚未实现"}})
def stream_chat(session_id: UUID, payload: ChatRequest, services: ServiceDependency, user_id: UserDependency):
    services.sessions.get(user_id, session_id, message_limit=1)
    raise AppError("NOT_IMPLEMENTED", "SSE 将在下一阶段实现，请使用非流式问答接口", 501)
