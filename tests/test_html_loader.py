import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.infrastructure.loaders.local import LocalDocumentLoader, validate_upload


def test_local_html_keeps_facts_and_table_rows_without_executing_resources(tmp_path):
    path = tmp_path / "rules.html"
    path.write_text('''<!doctype html><html><head><title>hidden</title>
      <script>fetch("https://example.invalid")</script></head><body>
      <h1>Warranty &amp; repair</h1><p>NX-001: every 6 months.</p>
      <table><tr><th>Model</th><th>Power</th></tr><tr><td>NX-001</td><td>100 W</td></tr></table>
      <script>invented 999 W</script><style>secret</style><template>excluded</template>
      <img src="https://example.invalid/image"><p>Contact support.</p></body></html>''', encoding="utf-8")
    assert validate_upload(path.name, "text/html; charset=utf-8") == (path.name, ".html")
    result = LocalDocumentLoader(Settings(_env_file=None)).load(path, ".html")[0].page_content
    assert "Warranty & repair\nNX-001: every 6 months." in result
    assert "NX-001 | 100 W" in result and "Contact support." in result
    assert all(word not in result for word in ("hidden", "fetch", "999", "secret", "excluded", "https://"))


def test_html_rejects_binary_wrong_mime_and_empty_script_only(tmp_path):
    loader = LocalDocumentLoader(Settings(_env_file=None))
    with pytest.raises(AppError, match="MIME"):
        validate_upload("rules.html", "application/javascript")
    path = tmp_path / "rules.htm"
    for content, code in [("abc\x00def", "INVALID_DOCUMENT"), ("<script>answer</script>", "EMPTY_DOCUMENT")]:
        path.write_text(content, encoding="utf-8")
        with pytest.raises(AppError) as error:
            loader.load(path, ".htm")
        assert error.value.code == code
