import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from uuid import UUID, uuid5

from docx import Document as WordDocument
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.core.config import Settings
from app.core.errors import AppError
from app.domain.contracts import Chunk

MIME_TYPES = {
    ".txt": {"text/plain"},
    ".md": {"text/plain", "text/markdown", "text/x-markdown"},
    ".pdf": {"application/pdf"},
    ".doc": {"application/msword"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".ppt": {"application/vnd.ms-powerpoint"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".xls": {"application/vnd.ms-excel"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}


def validate_upload(filename: str, content_type: str | None) -> tuple[str, str]:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(name).suffix.lower()
    if not name or len(name.encode("utf-8")) > 255 or any(ord(character) < 32 for character in name):
        raise AppError("INVALID_FILENAME", "文件名无效")
    if extension not in MIME_TYPES:
        raise AppError("UNSUPPORTED_FILE_TYPE", "仅支持 PDF、MD、TXT、Word、PowerPoint 和 Excel", 415)
    mime = (content_type or "application/octet-stream").split(";", 1)[0].lower()
    if mime not in MIME_TYPES[extension] | {"application/octet-stream"}:
        raise AppError("INVALID_MIME_TYPE", "文件扩展名与 MIME 类型不匹配", 415)
    return name, extension


def validate_document_container(path: Path, extension: str) -> None:
    if extension == ".pdf":
        with path.open("rb") as source:
            if not source.read(1024).lstrip().startswith(b"%PDF-"):
                raise AppError("INVALID_DOCUMENT", "PDF 文件签名无效")
        try:
            reader = PdfReader(path)
            if reader.is_encrypted or len(reader.pages) > 1000:
                raise AppError("INVALID_DOCUMENT", "暂不支持加密 PDF 或超过 1000 页的文档")
        except AppError:
            raise
        except Exception:
            raise AppError("INVALID_DOCUMENT", "PDF 文件损坏或无法解析") from None
        return
    if extension in {".doc", ".ppt", ".xls"}:
        with path.open("rb") as source:
            if source.read(8) != bytes.fromhex("d0cf11e0a1b11ae1"):
                raise AppError("INVALID_DOCUMENT", "旧版 Office 文件签名无效")
        return
    markers = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
    }
    marker = markers.get(extension)
    if marker is None:
        return
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(entry.file_size for entry in entries) > 100 * 1024 * 1024:
                raise AppError("DOCUMENT_TOO_LARGE", "Office 容器解压大小超出限制", 413)
            if marker not in {entry.filename.replace("\\", "/") for entry in entries}:
                raise AppError("INVALID_DOCUMENT", "Office 容器类型与扩展名不匹配")
    except AppError:
        raise
    except (OSError, zipfile.BadZipFile):
        raise AppError("INVALID_DOCUMENT", "Office 文件容器损坏") from None


class LocalDocumentLoader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self, path: Path, extension: str) -> list[Document]:
        try:
            documents = self._load(path, extension)
            total = sum(len(document.page_content) for document in documents)
            if total > self.settings.max_document_characters:
                raise AppError("DOCUMENT_TOO_LARGE", "解析后的文本超过长度限制", 413)
            if not any(document.page_content.strip() for document in documents):
                raise AppError("EMPTY_DOCUMENT", "未提取到文本；扫描件需先 OCR，MinerU 云解析尚未接入")
            return documents
        except AppError:
            raise
        except Exception:
            raise AppError("INVALID_DOCUMENT", "文件损坏、编码不支持或无法解析") from None

    def _load(self, path: Path, extension: str) -> list[Document]:
        if extension in {".txt", ".md"}:
            content = path.read_text(encoding="utf-8-sig")
            if "\x00" in content:
                raise AppError("INVALID_DOCUMENT", "文本包含无效二进制内容")
            if extension == ".md":
                sections = []
                title = None
                lines = []
                fenced = False
                for line in content.splitlines():
                    if line.lstrip().startswith((chr(96) * 3, "~~~")):
                        fenced = not fenced
                    if not fenced and re.match(r"^#{1,6}\s+", line):
                        if lines:
                            sections.append(Document(page_content="\n".join(lines), metadata={"section": title}))
                        title = line.lstrip("# ").strip()[:500]
                        lines = []
                    lines.append(line)
                if lines:
                    sections.append(Document(page_content="\n".join(lines), metadata={"section": title}))
                return sections
            return [Document(page_content=content)]
        if extension == ".pdf":
            validate_document_container(path, extension)
            reader = PdfReader(path)
            documents = []
            length = 0
            for page_number, page in enumerate(reader.pages, 1):
                content = page.extract_text() or ""
                length += len(content)
                if length > self.settings.max_document_characters:
                    raise AppError("DOCUMENT_TOO_LARGE", "PDF 文本超过长度限制", 413)
                documents.append(Document(page_content=content, metadata={"page_number": page_number}))
            return documents
        if extension == ".docx":
            with zipfile.ZipFile(path) as archive:
                if sum(entry.file_size for entry in archive.infolist()) > 100 * 1024 * 1024:
                    raise AppError("DOCUMENT_TOO_LARGE", "DOCX 解压大小超出限制", 413)
            document = WordDocument(path)
            blocks = []
            for block in document.iter_inner_content():
                if hasattr(block, "text"):
                    blocks.append(block.text)
                else:
                    blocks.extend(" | ".join(cell.text for cell in row.cells) for row in block.rows)
            return [Document(page_content="\n\n".join(blocks))]
        if extension == ".doc":
            return self._convert_doc(path)
        raise AppError("UNSUPPORTED_FILE_TYPE", "不支持的文件类型", 415)

    def _convert_doc(self, path: Path) -> list[Document]:
        validate_document_container(path, ".doc")
        with tempfile.TemporaryDirectory(prefix="rag-doc-") as temporary:
            directory = Path(temporary)
            try:
                subprocess.run(
                    [self.settings.libreoffice_path, f"-env:UserInstallation={(directory / 'profile').as_uri()}",
                     "--headless", "--convert-to", "docx", "--outdir", str(directory), str(path.resolve())],
                    timeout=self.settings.conversion_timeout, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError:
                raise AppError("CONVERTER_UNAVAILABLE", "请安装 LibreOffice 并配置 LIBREOFFICE_PATH", 503) from None
            except subprocess.TimeoutExpired:
                raise AppError("CONVERSION_TIMEOUT", "DOC 转换超时", 504) from None
            converted = directory / f"{path.stem}.docx"
            if not converted.is_file():
                raise AppError("CONVERSION_FAILED", "未生成有效 DOCX 文件")
            return self._load(converted, ".docx")

    def chunks(self, documents: list[Document], user_id: UUID, document_id: UUID, source_name: str) -> list[Chunk]:
        splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base", chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", ". ", " ", ""],
            disallowed_special=(),
        )
        cleaned = [
            Document(page_content=re.sub(r"\n{3,}", "\n\n", document.page_content.replace("\r\n", "\n").replace("\x00", "")).strip(), metadata=document.metadata)
            for document in documents if document.page_content.strip()
        ]
        parts = splitter.split_documents(cleaned)
        if len(parts) > self.settings.max_document_chunks:
            raise AppError("TOO_MANY_CHUNKS", "文档切片数量超出限制", 413)
        return [
            Chunk(
                chunk_id=str(uuid5(document_id, f"{index}:{part.page_content}")),
                user_id=str(user_id), document_id=str(document_id), chunk_index=index,
                content=part.page_content, source_name=source_name,
                page_number=part.metadata.get("page_number"), section=part.metadata.get("section"),
            )
            for index, part in enumerate(parts)
        ]
