from pathlib import Path

import pytest
from pydantic import SecretStr

from telegram_mcp.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_id=1,
        api_hash=SecretStr("test-hash"),
        data_dir=tmp_path,
        _env_file=None,  # type: ignore[call-arg]  # keep a developer's real .env out of tests
    )
