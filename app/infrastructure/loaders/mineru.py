import io
import json
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

    def normalize(self, items: list[dict], document_type: str) -> list[Document]:
        if document_type not in {"pdf", "powerpoint", "word", "excel"} or not isinstance(items, list):
            raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 输出结构不受支持", 502)
        documents, section = [], None
        for item in items:
            if not isinstance(item, dict):
                raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 输出结构不受支持", 502)
            kind = item.get("type")
            content = item.get("text") or item.get("content")
            if kind == "title" and isinstance(content, str) and content.strip():
                section = content.strip()[:2048]
                continue
            if kind == "table":
                raw_table = item.get("table_body") or item.get("html") or content
                content = table_text(raw_table) if isinstance(raw_table, str) else None
            if kind in {"equation", "formula"}:
                content = item.get("latex") or content
            if not isinstance(content, str) or not content.strip():
                continue
            metadata = {"section": section}
            if document_type == "pdf":
                if not isinstance(item.get("page_idx"), int) or item["page_idx"] < 0:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "PDF 输出缺少可靠页码", 502)
                metadata["page_number"] = item["page_idx"] + 1
            elif document_type == "powerpoint":
                if not isinstance(item.get("slide_idx"), int) or item["slide_idx"] < 0:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "PowerPoint 输出缺少可靠幻灯片编号", 502)
                metadata["page_number"] = item["slide_idx"] + 1
            elif document_type == "excel":
                sheet = item.get("sheet_name") or item.get("section")
                if not isinstance(sheet, str) or not sheet.strip():
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "Excel 输出缺少工作表边界", 502)
                metadata["section"] = sheet.strip()[:2048]
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
                payload = response.json()
                data = payload["data"]
                batch_id, upload_url = data["batch_id"], data["file_urls"][0]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                raise AppError("MINERU_SUBMISSION_UNKNOWN", "MinerU 任务提交状态无法确认，请稍后手动重试", 502) from None
            if response.status_code != 200 or payload.get("code") != 0 or not signed_url_allowed(upload_url):
                raise AppError("MINERU_SUBMISSION_FAILED", "MinerU 拒绝了解析任务", 502)
            if on_submitted:
                on_submitted(batch_id)
            try:
                with path.open("rb") as source:
                    upload = self.client.put(upload_url, content=source, timeout=self.settings.mineru_timeout)
                if upload.status_code not in {200, 201, 204}:
                    raise AppError("MINERU_UPLOAD_FAILED", "文件上传至 MinerU 失败", 502)
            except httpx.HTTPError:
                raise AppError("MINERU_UPLOAD_FAILED", "文件上传至 MinerU 失败", 502) from None
            if on_uploaded:
                on_uploaded()
        deadline = time.monotonic() + self.settings.mineru_parse_timeout
        interval = 3.0
        while time.monotonic() < deadline:
            try:
                result = self.client.get(self.result_endpoint.format(batch_id=batch_id), headers=headers, timeout=self.settings.mineru_timeout)
                body = result.json()
                entries = body.get("data", {}).get("extract_result", [])
                entry = entries[0] if entries else {}
                state = str(entry.get("state", "")).lower()
                if state in {"done", "completed", "success"}:
                    zip_url = entry.get("full_zip_url") or entry.get("zip_url")
                    if not isinstance(zip_url, str) or not signed_url_allowed(zip_url):
                        raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果地址无效", 502)
                    return self._read_result(self._download(zip_url), document_type)
                if state in {"failed", "error"}:
                    raise AppError("MINERU_PARSE_FAILED", "MinerU 文档解析失败", 502)
            except AppError:
                raise
            except (httpx.HTTPError, ValueError, TypeError):
                interval = min(interval * 2, 15)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise AppError("MINERU_PARSE_TIMEOUT", "等待 MinerU 解析超时，远端任务可能仍在运行", 504)

    def _download(self, url: str) -> bytes:
        chunks, size = [], 0
        try:
            with self.client.stream("GET", url, timeout=self.settings.mineru_timeout) as response:
                if response.status_code != 200:
                    raise AppError("MINERU_DOWNLOAD_FAILED", "MinerU 结果下载失败", 502)
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise AppError("MINERU_RESULT_TOO_LARGE", "MinerU 结果压缩包超过限制", 413)
                    chunks.append(chunk)
        except httpx.HTTPError:
            raise AppError("MINERU_DOWNLOAD_FAILED", "MinerU 结果下载失败", 502) from None
        return b"".join(chunks)

    def _read_result(self, content: bytes, document_type: str) -> list[Document]:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 200 * 1024 * 1024:
                    raise AppError("MINERU_RESULT_TOO_LARGE", "MinerU 解压结果超过限制", 413)
                candidates = []
                for entry in entries:
                    path = PurePosixPath(entry.filename.replace("\\", "/"))
                    if path.is_absolute() or ".." in path.parts or ":" in entry.filename:
                        raise AppError("INVALID_MINERU_ARCHIVE", "MinerU 结果压缩包路径无效", 502)
                    if path.name.endswith(("content_list.json", "content_list_v2.json")):
                        candidates.append(entry)
                if not candidates:
                    raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果缺少结构化内容", 502)
                data = json.loads(archive.read(candidates[0]))
        except AppError:
            raise
        except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
            raise AppError("MINERU_OUTPUT_UNSUPPORTED", "MinerU 结果无法解析", 502) from None
        return self.normalize(data, document_type)

    def close(self) -> None:
        self.client.close()
