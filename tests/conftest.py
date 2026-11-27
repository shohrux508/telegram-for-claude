from pathlib import Path

import pytest
from pydantic import SecretStr

from telegram_mcp.config import Settings


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real settings.json, sessions and archive out of every test."""
    monkeypatch.setenv("TELEGRAM_MCP_DATA_DIR", str(tmp_path))
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_id=1,
        api_hash=SecretStr("test-hash"),
        data_dir=tmp_path,
        archive_mode="off",
        auto_sync=False,
        _env_file=None,  # type: ignore[call-arg]  # keep a developer's real .env out of tests
    )
