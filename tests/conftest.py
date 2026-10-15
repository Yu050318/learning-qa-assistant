"""Offline tests must not download tiktoken's vocabulary on a fresh machine."""

from unittest.mock import patch

import pytest

from app.application.evidence import _encoding


@pytest.fixture(autouse=True, scope="session")
def offline_tokenizer():
    _encoding.cache_clear()
    with patch("app.application.evidence.tiktoken.get_encoding", side_effect=RuntimeError("offline tests")):
        yield
    _encoding.cache_clear()
