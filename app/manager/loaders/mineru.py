import io
import json
import os
import shutil
import subprocess
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from langchain_core.documents import Document

from app.core.config import Settings
from app.core.errors import AppError


DOCUMENT_TYPES = {
    ".pdf": "pdf", ".doc": "word", ".docx": "word",
    ".ppt": "powerpoint", ".pptx": "powerpoint",
    ".xls": "excel", ".xlsx": "excel",
}


class TableTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = []

    def text(self) -> str:
        return "\n".join(" | ".join(row) for row in self.rows)


def table_text(value: str) -> str:
    parser = TableTextParser()
    parser.feed(value)
    return parser.text() or " ".join(value.split())


def signed_url_allowed(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and not parsed.username and not parsed.password and (
            host.endswith(".aliyuncs.com") or host.endswith(".mineru.net") or host.endswith(".openxlab.org.cn")
        )
    except ValueError:
        return False


class MinerUParser:
    submit_endpoint = "https://mineru.net/api/v4/file-urls/batch"
    result_endpoint = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.Client(transport=transport, follow_redirects=False)
        self.native_tls_fallback = transport is None

    @staticmethod
    def _content_text(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(MinerUParser._content_text(item) for item in value)
        if isinstance(value, dict):
            for key in ("title_content", "paragraph_content", "table_content", "equation_content", "content", "text", "html", "latex", "value"):
                if key in value:
                    return MinerUParser._content_text(value[key])
        return ""

    @staticmethod
    def _flatten_items(items: list) -> list[dict]:
        flattened = []
        for page_idx, value in enumerate(items):
            page_items = value if isinstance(value, list) else [value]
            for item in page_items:
                if not isinstance(item, dict):
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 输出结构不受支持", 502)
                item = dict(item)
                if isinstance(value, list):
                    item.setdefault("page_idx", page_idx)
                flattened.append(item)
        return flattened

    def normalize(self, items: list, document_type: str) -> list[Document]:
        if document_type not in {"pdf", "powerpoint", "word", "excel"} or not isinstance(items, list):
            raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 输出结构不受支持", 502)
        documents, section = [], None
        for item in self._flatten_items(items):
            kind = item.get("type")
            content = self._content_text(item.get("text") or item.get("content"))
            is_title = kind == "title" or (
                kind == "text" and isinstance(item.get("text_level"), int) and item["text_level"] > 0
            )
            if is_title and content.strip():
                section = content.strip()[:2048]
                continue
            if kind == "list" and isinstance(item.get("list_items"), list):
                content = "\n".join(filter(None, (self._content_text(value).strip() for value in item["list_items"])))
            if kind == "table":
                raw_table = self._content_text(item.get("table_body") or item.get("html") or content)
                content = table_text(raw_table) if isinstance(raw_table, str) else None
            if kind in {"equation", "formula"}:
                content = self._content_text(item.get("latex") or content)
            if not isinstance(content, str) or not content.strip():
                continue
            metadata = {"section": section}
            if document_type == "pdf":
                if not isinstance(item.get("page_idx"), int) or item["page_idx"] < 0:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "PDF 输出缺少可靠页码", 502)
                metadata["page_number"] = item["page_idx"] + 1
            elif document_type == "powerpoint":
                page_idx = item.get("page_idx", item.get("slide_idx"))
                if not isinstance(page_idx, int) or page_idx < 0:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "PowerPoint 输出缺少可靠幻灯片编号", 502)
                metadata["page_number"] = page_idx + 1
            elif document_type == "excel":
                sheet = item.get("sheet_name") or item.get("section")
                if isinstance(sheet, str) and sheet.strip():
                    metadata["section"] = sheet.strip()[:2048]
                if isinstance(item.get("page_idx"), int) and item["page_idx"] >= 0:
                    metadata["page_number"] = item["page_idx"] + 1
            documents.append(Document(page_content=content.strip(), metadata=metadata))
        if not documents:
            raise AppError("EMPTY_DOCUMENT", "MinerU 未提取到可用文本")
        return documents

    def parse(self, path: Path, extension: str, attempt_id: str, on_submitted=None, on_uploaded=None, batch_id: str | None = None) -> list[Document]:
        if not self.settings.mineru_enabled or not self.settings.mineru_api_token.get_secret_value():
            raise AppError("MINERU_NOT_CONFIGURED", "PDF/Office 解析需要启用并配置 MinerU", 503)
        document_type = DOCUMENT_TYPES.get(extension)
        if not document_type:
            raise AppError("UNSUPPORTED_FILE_TYPE", "MinerU 不支持此文件类型", 415)
        headers = {"Authorization": f"Bearer {self.settings.mineru_api_token.get_secret_value()}"}
        if batch_id is None:
            try:
                response = self.client.post(
                    self.submit_endpoint,
                    headers=headers,
                    json={"files": [{"name": f"{attempt_id}{extension}", "data_id": attempt_id}], "model_version": self.settings.mineru_model_version},
                    timeout=self.settings.mineru_timeout,
                )
                payload = self._api_payload(response, extension)
                data = payload["data"]
                batch_id, upload_url = data["batch_id"], data["file_urls"][0]
            except AppError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                raise AppError("MINERU_SUBMISSION_UNKNOWN", "MinerU 任务提交状态无法确认，请稍后手动重试", 502) from None
            if not signed_url_allowed(upload_url):
                raise AppError("MINERU_SUBMISSION_FAILED", "MinerU 拒绝了解析任务", 502)
            if on_submitted:
                on_submitted(batch_id)
            self._upload(path, upload_url)
            if on_uploaded:
                on_uploaded()
        deadline = time.monotonic() + self.settings.mineru_parse_timeout
        interval = 3.0
        while time.monotonic() < deadline:
            try:
                result = self.client.get(self.result_endpoint.format(batch_id=batch_id), headers=headers, timeout=self.settings.mineru_timeout)
                body = self._api_payload(result, extension)
                entries = body.get("data", {}).get("extract_result", [])
                entry = entries[0] if entries else {}
                state = str(entry.get("state", "")).lower()
                if state in {"done", "completed", "success"}:
                    zip_url = entry.get("full_zip_url") or entry.get("zip_url")
                    if not isinstance(zip_url, str) or not signed_url_allowed(zip_url):
                        raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果地址无效", 502)
                    return self._read_result(self._download(zip_url), document_type)
                if state in {"failed", "error"}:
                    error_message = str(entry.get("err_msg") or entry.get("error_msg") or "").lower()
                    if "number of pages exceeds limit" in error_message or "页数超过" in error_message:
                        raise self._page_limit_error(extension)
                    raise AppError("MINERU_PARSE_FAILED", "MinerU 文档解析失败", 502)
            except AppError:
                raise
            except (httpx.HTTPError, ValueError, TypeError):
                interval = min(interval * 2, 15)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise AppError("MINERU_PARSE_TIMEOUT", "等待 MinerU 解析超时，远端任务可能仍在运行", 504)

    @staticmethod
    def _page_limit_error(extension: str) -> AppError:
        return AppError(
            "MINERU_PAGE_LIMIT_EXCEEDED",
            "PDF 超过 MinerU 的 200 页限制，请拆分后重试" if extension == ".pdf"
            else "文档超过 MinerU 的 200 页限制，请拆分后重试",
            422,
        )

    def _api_payload(self, response: httpx.Response, extension: str) -> dict:
        if response.status_code in {401, 403}:
            raise AppError("MINERU_AUTH_FAILED", "MinerU Token 无效或已过期，请更新配置", 502)
        if response.status_code == 429:
            raise AppError("MINERU_RATE_LIMITED", "MinerU 请求过于频繁，请稍后重试", 503)
        if response.status_code >= 500:
            raise AppError("MINERU_UPSTREAM_UNAVAILABLE", "MinerU 服务暂时不可用，请稍后重试", 503)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise AppError("MINERU_API_ERROR", "MinerU 返回了无法识别的响应", 502) from None
        if not isinstance(payload, dict):
            raise AppError("MINERU_API_ERROR", "MinerU 返回了无法识别的响应", 502)
        code = payload.get("code")
        if str(code) in {"A0202", "A0211"}:
            raise AppError("MINERU_AUTH_FAILED", "MinerU Token 无效或已过期，请更新配置", 502)
        if str(code) == "-60006":
            raise self._page_limit_error(extension)
        if str(code) == "-60001":
            raise AppError("MINERU_UNSUPPORTED_FORMAT", "MinerU 不支持该文件格式或文件内容无效", 415)
        if response.status_code != 200 or code not in {None, 0, "0"}:
            suffix = f"（错误码 {code}）" if code is not None else ""
            raise AppError("MINERU_API_ERROR", f"MinerU 请求失败{suffix}", 502)
        return payload

    def _upload(self, path: Path, upload_url: str) -> None:
        last_error = None
        for attempt in range(3):
            try:
                with path.open("rb") as source:
                    upload = self.client.put(upload_url, content=source, timeout=self.settings.mineru_timeout)
                if upload.status_code in {200, 201, 204}:
                    return
                if upload.status_code in {401, 403}:
                    raise AppError("MINERU_UPLOAD_AUTH_FAILED", "MinerU 上传地址已失效或签名无效，请重试", 502)
                if upload.status_code not in {429} and upload.status_code < 500:
                    raise AppError("MINERU_UPLOAD_FAILED", f"MinerU 拒绝接收文件（HTTP {upload.status_code}），请重试", 502)
                last_error = upload.status_code
            except AppError:
                raise
            except httpx.HTTPError as error:
                last_error = error
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
        if isinstance(last_error, httpx.TimeoutException):
            raise AppError("MINERU_UPLOAD_TIMEOUT", "连接 MinerU 存储超时，请检查网络后重试", 504) from None
        if last_error == 429:
            raise AppError("MINERU_RATE_LIMITED", "MinerU 请求过于频繁，请稍后重试", 503)
        if isinstance(last_error, int) and last_error >= 500:
            raise AppError("MINERU_UPSTREAM_UNAVAILABLE", "MinerU 存储服务暂时不可用，请稍后重试", 503)
        raise AppError("MINERU_UPLOAD_FAILED", "无法连接 MinerU 存储服务，请检查网络后重试", 502)

    def _download(self, url: str) -> bytes:
        limit = 100 * 1024 * 1024
        last_error = None
        for attempt in range(3):
            chunks, size = [], 0
            try:
                with self.client.stream("GET", url, timeout=self.settings.mineru_timeout) as response:
                    if response.status_code in {401, 403}:
                        raise AppError("MINERU_DOWNLOAD_URL_EXPIRED", "MinerU 结果下载地址已失效，请重新处理", 502)
                    if response.status_code != 200:
                        if response.status_code == 429 or response.status_code >= 500:
                            last_error = response.status_code
                        else:
                            raise AppError(
                                "MINERU_DOWNLOAD_FAILED",
                                f"MinerU 拒绝下载结果（HTTP {response.status_code}）",
                                502,
                            )
                    else:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > limit:
                                raise AppError("MINERU_RESULT_TOO_LARGE", "MinerU 结果压缩包超过限制", 413)
                            chunks.append(chunk)
                        return b"".join(chunks)
            except AppError:
                raise
            except httpx.HTTPError as error:
                last_error = error
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))

        if self._is_tls_error(last_error):
            fallback = self._download_with_windows_curl(url, limit) if self.native_tls_fallback else None
            if fallback is not None:
                return fallback
            raise AppError(
                "MINERU_DOWNLOAD_TLS_FAILED",
                "连接 MinerU 结果存储时 TLS 握手失败，请检查代理分流或网络后重试",
                502,
            )
        if isinstance(last_error, httpx.TimeoutException):
            raise AppError("MINERU_DOWNLOAD_TIMEOUT", "下载 MinerU 结果超时，请检查网络后重试", 504)
        if last_error == 429:
            raise AppError("MINERU_RATE_LIMITED", "MinerU 请求过于频繁，请稍后重试", 503)
        if isinstance(last_error, int) and last_error >= 500:
            raise AppError("MINERU_UPSTREAM_UNAVAILABLE", "MinerU 结果存储服务暂时不可用，请稍后重试", 503)
        raise AppError("MINERU_DOWNLOAD_FAILED", "无法连接 MinerU 结果存储服务，请检查网络后重试", 502)

    @staticmethod
    def _is_tls_error(error) -> bool:
        message = str(error).lower()
        return isinstance(error, httpx.ConnectError) and any(
            marker in message for marker in ("ssl", "tls", "handshake", "certificate")
        )

    def _download_with_windows_curl(self, url: str, limit: int) -> bytes | None:
        executable = shutil.which("curl.exe") if os.name == "nt" else None
        if not executable or any(ord(character) < 32 for character in url):
            return None
        escaped_url = url.replace("\\", "\\\\").replace('"', '\\"')
        try:
            result = subprocess.run(
                [
                    executable, "--config", "-", "--fail", "--silent", "--show-error",
                    "--proto", "=https", "--max-time", str(self.settings.mineru_timeout),
                    "--max-filesize", str(limit), "--output", "-",
                ],
                input=f'url = "{escaped_url}"\n'.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.settings.mineru_timeout + 5,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        if len(result.stdout) > limit:
            raise AppError("MINERU_RESULT_TOO_LARGE", "MinerU 结果压缩包超过限制", 413)
        return result.stdout

    def _read_result(self, content: bytes, document_type: str) -> list[Document]:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 200 * 1024 * 1024:
                    raise AppError("MINERU_RESULT_TOO_LARGE", "MinerU 解压结果超过限制", 413)
                v1_candidates, v2_candidates = [], []
                for entry in entries:
                    path = PurePosixPath(entry.filename.replace("\\", "/"))
                    if path.is_absolute() or ".." in path.parts or ":" in entry.filename:
                        raise AppError("INVALID_MINERU_ARCHIVE", "MinerU 结果压缩包路径无效", 502)
                    if path.name.endswith("content_list.json"):
                        v1_candidates.append(entry)
                    elif path.name.endswith("content_list_v2.json"):
                        v2_candidates.append(entry)
                candidates = v1_candidates or v2_candidates
                if not candidates:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果缺少结构化内容", 502)
                selected = min(candidates, key=lambda entry: entry.filename)
                data = json.loads(archive.read(selected))
        except AppError:
            raise
        except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果无法解析", 502) from None
        return self.normalize(data, document_type)

    def close(self) -> None:
        self.client.close()
