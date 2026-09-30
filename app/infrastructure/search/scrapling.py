import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit

from scrapling.fetchers import Fetcher

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import WebSearchResult
from app.infrastructure.search.tavily import safe_public_url

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ScraplingWebReader:
    def __init__(self, settings: Settings, fetch=None, resolve=None):
        self.settings = settings
        self.fetch = fetch or Fetcher.get
        self.resolve = resolve or socket.getaddrinfo

    def _validate_url(self, value: str) -> None:
        if len(value) > 2048 or not safe_public_url(value):
            raise AppError("UNSAFE_WEB_URL", "网页地址不安全或格式无效", 403)
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise AppError("UNSAFE_WEB_URL", "网页地址端口无效", 403) from None
        if port not in {None, 80, 443}:
            raise AppError("UNSAFE_WEB_URL", "网页地址端口不允许", 403)
        port = port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = {
                ipaddress.ip_address(item[4][0].split("%", 1)[0])
                for item in self.resolve(parsed.hostname, port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError, TypeError):
            raise AppError("WEB_PAGE_UNAVAILABLE", "网页域名无法安全解析", 502) from None
        if not addresses or any(not address.is_global for address in addresses):
            raise AppError("UNSAFE_WEB_URL", "网页地址解析到非公网地址", 403)

    def read(self, url: str, timeout_seconds: float) -> WebSearchResult:
        self._validate_url(url)
        try:
            response = self.fetch(
                url,
                timeout=min(timeout_seconds, self.settings.scrapling_timeout),
                retries=1,
                follow_redirects="safe",
                max_redirects=5,
                verify=True,
            )
            final_url = str(response.url)
            self._validate_url(final_url)
            if response.status < 200 or response.status >= 300:
                raise AppError("WEB_PAGE_BAD_STATUS", f"网页返回 HTTP {response.status}", 502)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
                raise AppError("WEB_PAGE_UNSUPPORTED", "网页响应不是 HTML", 415)
            if len(response.body) > _MAX_RESPONSE_BYTES:
                raise AppError("WEB_PAGE_TOO_LARGE", "网页内容超过大小限制", 413)
            content = response.markdown(main_content_only=True).strip()
            if not content:
                raise AppError("EMPTY_WEB_PAGE", "网页未提取到可用正文", 422)
            title = (response.css("title::text").get() or urlsplit(final_url).hostname or final_url).strip()[:500]
            return WebSearchResult(
                title=title,
                url=final_url,
                content=content[: self.settings.scrapling_max_characters],
                score=None,
                retrieved_at=datetime.now(timezone.utc),
            )
        except AppError:
            raise
        except Exception:
            raise AppError("WEB_PAGE_UNAVAILABLE", "网页抓取失败", 502) from None
