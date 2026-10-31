from app.manager.transactions import transaction_scope, as_transaction
import ipaddress
import socket
from time import monotonic
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from curl_cffi import CurlOpt
from curl_cffi.requests import Session
from scrapling.engines.toolbelt.custom import Response

from app.core.config import Settings
from app.core.errors import AppError
from app.entity.bo.contracts import WebSearchResult
from app.core.urls import safe_public_url

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def pinned_public_session(url, addresses):
    """Preserve the vetted destination and TLS hostname; ignore proxy env vars."""
    parsed = urlsplit(url)
    address = min(addresses, key=lambda value: (value.version, str(value)))
    ip = f"[{address}]" if address.version == 6 else str(address)
    resolve = f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}:{ip}"
    return Session(curl_options={CurlOpt.RESOLVE: [resolve.encode()], CurlOpt.PROXY: b""}, trust_env=False)


class ScraplingWebReader:
    def __init__(self, settings: Settings, fetch=None, resolve=None):
        self.settings = settings
        self.fetch = fetch
        self.resolve = resolve or socket.getaddrinfo

    def _validate_url(self, value: str) -> set:
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
        return addresses

    @staticmethod
    def _fetch_pinned(url, addresses, timeout):
        """Keep Scrapling parsing, pin its curl transport to the vetted DNS answer."""
        chunks, size, too_large = [], 0, False

        def receive(chunk):
            nonlocal size, too_large
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                too_large = True
                return 0
            chunks.append(chunk)
            return len(chunk)

        try:
            # Disable environment proxies: they would bypass the vetted destination.
            with pinned_public_session(url, addresses) as session:
                response = session.get(url, timeout=timeout, allow_redirects=False, verify=True,
                                       impersonate="chrome", content_callback=receive)
        except Exception:
            if too_large:
                raise AppError("WEB_PAGE_TOO_LARGE", "网页内容超过大小限制", 413) from None
            raise
        return Response(url=response.url, content=b"".join(chunks), status=response.status_code,
                        reason=response.reason, cookies={}, headers=dict(response.headers), request_headers={},
                        encoding=response.encoding or "utf-8")

    def read(self, url: str, timeout_seconds: float) -> WebSearchResult:
        deadline = monotonic() + min(timeout_seconds, self.settings.scrapling_timeout)
        try:
            for hop in range(6):
                addresses = self._validate_url(url)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise AppError("WEB_PAGE_TIMEOUT", "网页抓取超时", 504)
                response = self.fetch(url, timeout=remaining, retries=1, follow_redirects=False, verify=True) if self.fetch else self._fetch_pinned(url, addresses, remaining)
                headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
                if response.status not in {301, 302, 303, 307, 308}:
                    break
                if hop == 5 or not headers.get("location"):
                    raise AppError("WEB_PAGE_UNAVAILABLE", "网页重定向次数过多或目标无效", 502)
                url = urljoin(url, headers["location"])
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
