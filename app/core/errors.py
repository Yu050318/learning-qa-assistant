class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class UpstreamError(AppError):
    def __init__(self, service: str, retryable: bool = False):
        super().__init__(f"{service.upper()}_UNAVAILABLE", f"{service} 服务不可用，请检查配置和服务状态", 503)
        self.retryable = retryable


def not_found() -> AppError:
    return AppError("NOT_FOUND", "资源不存在", 404)
