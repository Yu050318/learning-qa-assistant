import unittest
import json
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from langchain_core.documents import Document

from app.application.ingestion import IngestionService, prepare_retry_metadata
from app.core.errors import AppError
from app.core.config import Settings
from app.infrastructure.loaders.mineru import MinerUParser
from app.infrastructure.loaders.local import validate_document_container, validate_upload


class MinerUTests(unittest.TestCase):
    def setUp(self):
        self.parser = MinerUParser(Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"))

    def tearDown(self):
        self.parser.close()

    def test_upload_whitelist_includes_office_formats(self):
        for name, mime in (
            ("a.ppt", "application/vnd.ms-powerpoint"),
            ("a.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ("a.xls", "application/vnd.ms-excel"),
            ("a.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ):
            self.assertEqual(name[name.rfind("."):], validate_upload(name, mime)[1])

    def test_office_upload_requires_enabled_mineru_before_database_write(self):
        service = IngestionService(
            Settings(_env_file=None, mineru_enabled=False, mineru_api_token=""),
            None, None, None, None, None,
        )
        with self.assertRaises(AppError) as caught:
            service.upload(uuid4(), "a.pdf", "application/pdf", BytesIO(b"%PDF-1.7"))
        self.assertEqual("MINERU_NOT_CONFIGURED", caught.exception.code)

    def test_normalized_cache_roundtrip_preserves_source_metadata(self):
        with TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, parsed_dir=Path(directory))
            service = IngestionService(settings, None, None, None, None, None)
            document_id = uuid4()
            expected = [Document(page_content="正文", metadata={"page_number": 2, "section": "标题"})]
            service._save_cache(document_id, "hash", "pdf", expected)
            loaded = service._load_cache(document_id, "hash", "pdf")
            self.assertEqual(expected, loaded)

    def test_normalizes_pdf_and_ppt_page_numbers(self):
        pdf = self.parser.normalize([{"type": "text", "text": "PDF", "page_idx": 0}], "pdf")
        ppt = self.parser.normalize([{"type": "text", "text": "PPT", "page_idx": 2}], "powerpoint")
        self.assertEqual(1, pdf[0].metadata["page_number"])
        self.assertEqual(3, ppt[0].metadata["page_number"])

    def test_normalizes_text_level_list_items_and_v2_content(self):
        v1 = self.parser.normalize([
            {"type": "text", "text": "第一章", "text_level": 1, "page_idx": 0},
            {"type": "list", "list_items": ["第一项", "第二项"], "page_idx": 0},
        ], "pdf")
        v2 = self.parser.normalize([[
            {"type": "paragraph", "content": {"paragraph_content": [
                {"type": "text", "content": "V2 正文"},
            ]}},
        ]], "pdf")

        self.assertEqual("第一章", v1[0].metadata["section"])
        self.assertEqual("第一项\n第二项", v1[0].page_content)
        self.assertEqual("V2 正文", v2[0].page_content)
        self.assertEqual(1, v2[0].metadata["page_number"])

    def test_normalizes_word_section_and_excel_sheet(self):
        word = self.parser.normalize([
            {"type": "title", "text": "第一章"}, {"type": "text", "text": "正文"},
        ], "word")
        excel = self.parser.normalize([{
            "type": "table", "sheet_name": "销售", "table_body": "<table><tr><th>月份</th><th>金额</th></tr><tr><td>一月</td><td>10</td></tr></table>",
        }], "excel")
        self.assertEqual("第一章", word[0].metadata["section"])
        self.assertEqual("销售", excel[0].metadata["section"])
        self.assertIn("月份 | 金额", excel[0].page_content)

    def test_excel_content_does_not_require_an_undocumented_sheet_name(self):
        excel = self.parser.normalize([{
            "type": "table", "page_idx": 0,
            "table_body": "<table><tr><td>数据</td></tr></table>",
        }], "excel")

        self.assertEqual("数据", excel[0].page_content)
        self.assertEqual(1, excel[0].metadata["page_number"])

    def test_existing_batch_resumes_without_resubmitting_or_uploading(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("result_content_list.json", json.dumps([
                {"type": "text", "text": "恢复成功", "page_idx": 0},
            ]))

        def handler(request: httpx.Request):
            if request.method != "GET":
                raise AssertionError("resume must not submit or upload again")
            if request.url.host == "mineru.net":
                return httpx.Response(200, json={"data": {"extract_result": [{
                    "state": "done", "full_zip_url": "https://result.aliyuncs.com/output.zip",
                }]}})
            return httpx.Response(200, content=archive.getvalue())

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            documents = parser.parse(Path("unused.pdf"), ".pdf", "attempt", batch_id="existing-batch")
        finally:
            parser.close()

        self.assertEqual("恢复成功", documents[0].page_content)

    def test_page_limit_failure_has_actionable_message(self):
        def handler(request: httpx.Request):
            return httpx.Response(200, json={"data": {"extract_result": [{
                "state": "failed",
                "err_msg": "number of pages exceeds limit (200 pages), please split the file and try again",
            }]}})

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AppError) as caught:
                parser.parse(Path("unused.pdf"), ".pdf", "attempt", batch_id="existing-batch")
        finally:
            parser.close()

        self.assertEqual("MINERU_PAGE_LIMIT_EXCEEDED", caught.exception.code)
        self.assertEqual("PDF 超过 MinerU 的 200 页限制，请拆分后重试", caught.exception.message)

    def test_upload_rejection_has_actionable_message(self):
        def handler(request: httpx.Request):
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "data": {
                    "batch_id": "batch",
                    "file_urls": ["https://bucket.aliyuncs.com/file.pdf?signature=safe"],
                }})
            return httpx.Response(403)

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "file.pdf"
                path.write_bytes(b"%PDF-1.7")
                with self.assertRaises(AppError) as caught:
                    parser.parse(path, ".pdf", "attempt")
        finally:
            parser.close()

        self.assertEqual("MINERU_UPLOAD_AUTH_FAILED", caught.exception.code)
        self.assertEqual("MinerU 上传地址已失效或签名无效，请重试", caught.exception.message)

    def test_upload_retries_a_transient_storage_failure(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("result_content_list.json", json.dumps([
                {"type": "text", "text": "上传成功", "page_idx": 0},
            ]))
        puts = 0

        def handler(request: httpx.Request):
            nonlocal puts
            if request.method == "POST":
                return httpx.Response(200, json={"code": 0, "data": {
                    "batch_id": "batch",
                    "file_urls": ["https://bucket.aliyuncs.com/file.pdf?signature=safe"],
                }})
            if request.method == "PUT":
                puts += 1
                if puts == 1:
                    raise httpx.ConnectError("connection interrupted", request=request)
                return httpx.Response(200)
            if request.url.host == "mineru.net":
                return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                    "state": "done", "full_zip_url": "https://result.aliyuncs.com/output.zip",
                }]}})
            return httpx.Response(200, content=archive.getvalue())

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "file.pdf"
                path.write_bytes(b"%PDF-1.7")
                documents = parser.parse(path, ".pdf", "attempt")
        finally:
            parser.close()

        self.assertEqual(2, puts)
        self.assertEqual("上传成功", documents[0].page_content)

    def test_download_retries_a_transient_tls_failure(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("result_content_list.json", json.dumps([
                {"type": "text", "text": "下载成功", "page_idx": 0},
            ]))
        downloads = 0

        def handler(request: httpx.Request):
            nonlocal downloads
            if request.url.host == "mineru.net":
                return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                    "state": "done",
                    "full_zip_url": "https://cdn-mineru.openxlab.org.cn/output.zip",
                }]}})
            downloads += 1
            if downloads == 1:
                raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)
            return httpx.Response(200, content=archive.getvalue())

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            documents = parser.parse(Path("unused.pdf"), ".pdf", "attempt", batch_id="existing-batch")
        finally:
            parser.close()

        self.assertEqual(2, downloads)
        self.assertEqual("下载成功", documents[0].page_content)

    def test_persistent_download_tls_failure_is_actionable(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AppError) as caught:
                parser._download("https://cdn-mineru.openxlab.org.cn/output.zip")
        finally:
            parser.close()

        self.assertEqual("MINERU_DOWNLOAD_TLS_FAILED", caught.exception.code)
        self.assertIn("TLS 握手失败", caught.exception.message)

    def test_poll_api_error_does_not_become_parse_timeout(self):
        def handler(request: httpx.Request):
            return httpx.Response(200, json={"code": "A0211", "msg": "Token 过期"})

        parser = MinerUParser(
            Settings(
                _env_file=None, mineru_enabled=True, mineru_api_token="token",
                mineru_parse_timeout=0.01,
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AppError) as caught:
                parser.parse(Path("unused.pdf"), ".pdf", "attempt", batch_id="existing-batch")
        finally:
            parser.close()

        self.assertEqual("MINERU_AUTH_FAILED", caught.exception.code)

    def test_submission_api_error_without_data_is_preserved(self):
        def handler(request: httpx.Request):
            return httpx.Response(200, json={"code": -60006, "msg": "文件页数超过限制"})

        parser = MinerUParser(
            Settings(_env_file=None, mineru_enabled=True, mineru_api_token="token"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(AppError) as caught:
                parser.parse(Path("unused.pdf"), ".pdf", "attempt")
        finally:
            parser.close()

        self.assertEqual("MINERU_PAGE_LIMIT_EXCEEDED", caught.exception.code)

    def test_prefers_v1_content_list_when_archive_also_contains_v2(self):
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("a_content_list_v2.json", json.dumps([[{"type": "paragraph"}]]))
            output.writestr("z_content_list.json", json.dumps([
                {"type": "text", "text": "V1", "page_idx": 0},
            ]))

        documents = self.parser._read_result(archive.getvalue(), "pdf")

        self.assertEqual("V1", documents[0].page_content)

    def test_retry_preserves_timed_out_batch_but_replaces_failed_batch(self):
        timed_out = prepare_retry_metadata({
            "provider": "mineru", "generation": 2, "attempt_id": "attempt-2",
            "batch_id": "batch-2", "error_code": "MINERU_PARSE_TIMEOUT",
        })
        failed = prepare_retry_metadata({
            "provider": "mineru", "generation": 2, "attempt_id": "attempt-2",
            "batch_id": "batch-2", "error_code": "MINERU_PARSE_FAILED",
        })

        self.assertEqual("batch-2", timed_out["batch_id"])
        self.assertEqual(2, timed_out["generation"])
        self.assertIsNone(failed["batch_id"])
        self.assertEqual(3, failed["generation"])

    def test_retry_restarts_batch_if_process_stopped_during_upload(self):
        recovered = prepare_retry_metadata({
            "provider": "mineru", "phase": "uploading", "generation": 1,
            "attempt_id": "attempt-1", "batch_id": "batch-1", "error_code": None,
        })

        self.assertEqual("validating", recovered["phase"])
        self.assertEqual(2, recovered["generation"])
        self.assertIsNone(recovered["batch_id"])

    def test_ooxml_container_requires_type_specific_manifest(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "file.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", "<document/>")
            with self.assertRaises(AppError) as raised:
                validate_document_container(path, ".pptx")
            self.assertEqual("INVALID_DOCUMENT", raised.exception.code)

            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ppt/presentation.xml", "<presentation/>")
            validate_document_container(path, ".pptx")

    def test_legacy_office_container_requires_ole_signature(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "file.xls"
            path.write_bytes(b"not-an-ole-file")
            with self.assertRaises(AppError):
                validate_document_container(path, ".xls")
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"payload")
            validate_document_container(path, ".xls")


if __name__ == "__main__":
    unittest.main()
