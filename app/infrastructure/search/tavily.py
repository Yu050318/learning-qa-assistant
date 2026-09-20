import ipaddress
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import WebSearchBatch, WebSearchResult


def safe_public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        if parsed.hostname.lower() == "localhost":
            return False
        try:
            return not ipaddress.ip_address(parsed.hostname).is_private
        except ValueError:
            return True
    except ValueError:
        return False


class TavilySearch:
    endpoint = "https://api.tavily.com/search"

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(transport=transport, follow_redirects=False)

    def search(self, query: str, timeout_seconds: float) -> WebSearchBatch:
        if not self.settings.web_search_enabled or not self.settings.tavily_api_key.get_secret_value():
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索尚未配置", 503)
        payload = {
            "query": query, "topic": "general", "search_depth": "basic", "max_results": 5,
            "include_answer": False, "include_raw_content": False, "include_images": False,
            "auto_parameters": False, "include_usage": True,
        }
        try:
            response = self.client.post(
                self.endpoint, json=payload,
                headers={"Authorization": f"Bearer {self.settings.tavily_api_key.get_secret_value()}"},
                timeout=min(timeout_seconds, self.settings.tavily_timeout),
            )
        except httpx.TimeoutException:
            raise AppError("WEB_SEARCH_TIMEOUT", "网络搜索超时", 504) from None
        except httpx.HTTPError:
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索暂不可用", 502) from None
        if response.status_code in {401, 403}:
            raise AppError("WEB_SEARCH_AUTH_ERROR", "网络搜索认证失败", 502)
        if response.status_code in {429, 432, 433}:
            raise AppError("WEB_SEARCH_LIMITED", "网络搜索额度受限", 502)
        if response.status_code >= 500:
            raise AppError("WEB_SEARCH_UNAVAILABLE", "网络搜索暂不可用", 502)
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise AppError("INVALID_SEARCH_RESPONSE", "网络搜索返回无效结果", 502)
        try:
            data = response.json()
            raw_results = data["results"]
            if not isinstance(raw_results, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise AppError("INVALID_SEARCH_RESPONSE", "网络搜索返回无效结果", 502) from None
        received = datetime.now(timezone.utc)
        results, seen, characters = [], set(), 0
        for item in raw_results[:5]:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("title", "url", "content")):
                continue
            url, content = item["url"], item["content"].strip()[:3000]
            if not content or not safe_public_url(url) or url in seen or characters + len(content) > 15000:
                continue
            score = item.get("score")
            score = float(score) if isinstance(score, (int, float)) and math.isfinite(score) else None
            seen.add(url)
            characters += len(content)
            results.append(WebSearchResult(item["title"][:500], url, content, score, received))
        if raw_results and not results:
            raise AppError("INVALID_SEARCH_RESPONSE", "网络搜索没有可安全使用的结果", 502)
        usage = data.get("usage") if isinstance(data, dict) else None
        credits = usage.get("credits") if isinstance(usage, dict) and isinstance(usage.get("credits"), (int, float)) else None
        return WebSearchBatch(results, credits)

    def close(self) -> None:
        self.client.close()
