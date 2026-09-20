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
        service = IngestionService(Settings(_env_file=None), None, None, None, None, None)
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

    def test_normalizes_pdf_page_and_ppt_slide_numbers(self):
        pdf = self.parser.normalize([{"type": "text", "text": "PDF", "page_idx": 0}], "pdf")
        ppt = self.parser.normalize([{"type": "text", "text": "PPT", "slide_idx": 2}], "powerpoint")
        self.assertEqual(1, pdf[0].metadata["page_number"])
        self.assertEqual(3, ppt[0].metadata["page_number"])

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
