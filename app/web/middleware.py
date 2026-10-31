from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = -1
        if length < 0 or length > self.max_bytes:
            status = 400 if length < 0 else 413
            response = JSONResponse({"error": {"code": "INVALID_BODY_SIZE", "message": "请求体大小无效或超过限制", "request_id": scope.get("state", {}).get("request_id", "")}}, status_code=status)
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(413, "请求体超过大小限制")
            return message

        await self.app(scope, limited_receive, send)
