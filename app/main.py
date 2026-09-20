import logging
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from app.api.middleware import BodyLimitMiddleware
from app.api.routes import health_router, router, v2_router
from app.application.container import Services
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging

logger = logging.getLogger("rag")


def error_response(request: Request, code: str, message: str, status_code: int):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id}},
        status_code=status_code, headers={"X-Request-ID": request_id},
    )


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging()
        settings.configure_tracing()
        application.state.services = services or Services(settings)
        if hasattr(application.state.services, "start"):
            application.state.services.start()
        yield
        application.state.services.close()

    application = FastAPI(title="Learning Q&A Assistant", version="0.1.0", lifespan=lifespan)
    application.add_middleware(BodyLimitMiddleware, max_bytes=(settings.max_upload_mb + 1) * 1024 * 1024)

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception as error:
            logger.error("unexpected_error", extra={"request_id": request.state.request_id, "error_type": type(error).__name__})
            response = error_response(request, "INTERNAL_ERROR", "服务内部错误，请根据 request_id 检查日志", 500)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info("http_request", extra={"request_id": request.state.request_id, "user_hash": getattr(request.state, "user_hash", None), "elapsed_ms": round((perf_counter() - started) * 1000, 2), "status_code": response.status_code})
        return response

    @application.exception_handler(AppError)
    async def business_error(request: Request, error: AppError):
        return error_response(request, error.code, error.message, error.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return error_response(request, "VALIDATION_ERROR", "请求参数无效，请检查用户标识、UUID、文件和请求体", 422)

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        return error_response(request, f"HTTP_{error.status_code}", "请求无法处理", error.status_code)

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, error: SQLAlchemyError):
        sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
        if sqlstate == "55P03":
            return error_response(request, "RESOURCE_BUSY", "资源正在处理中，请稍后重试", 409)
        logger.error("database_error", extra={"request_id": request.state.request_id, "error_type": type(error).__name__})
        return error_response(request, "DATABASE_UNAVAILABLE", "数据库未就绪或尚未初始化", 503)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        logger.error("unexpected_error", extra={"request_id": request.state.request_id, "error_type": type(error).__name__})
        return error_response(request, "INTERNAL_ERROR", "服务内部错误，请根据 request_id 检查日志", 500)

    application.include_router(health_router)
    application.include_router(router)
    application.include_router(v2_router)
    return application


app = create_app()
