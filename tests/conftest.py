"""Offline tests must not download tiktoken's vocabulary on a fresh machine."""

import os
from unittest.mock import patch

import pytest

from app.application.evidence import _encoding

# pymilvus.settings calls load_dotenv() on import. Prevent it copying the real
# project .env into os.environ, which defeats Settings(_env_file=None) isolation.
# Explicit Pydantic dotenv loading and dedicated integration URLs still work.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


@pytest.fixture(autouse=True, scope="session")
def offline_tokenizer():
    _encoding.cache_clear()
    with patch("app.application.evidence.tiktoken.get_encoding", side_effect=RuntimeError("offline tests")):
        yield
    _encoding.cache_clear()
