import json
import sys
import types
from pathlib import Path

import pytest

from telegram_mcp import config
from telegram_mcp.config import Settings, read_settings_file, write_settings_file


def load() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_without_anything_the_api_is_not_configured_and_archive_is_local() -> None:
    settings = load()
    assert not settings.api_configured
    assert (settings.archive_mode, settings.archive_enabled) == ("local", True)


def test_settings_page_file_is_read(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"read_only": True, "hidden_chats": [5, -1001], "archive_mode": "off"}),
        encoding="utf-8",
    )
    settings = load()
    assert settings.read_only
    assert settings.hidden_chats == [5, -1001]
    assert not settings.archive_enabled


def test_environment_wins_over_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_settings_file({"read_only": True})
    monkeypatch.setenv("TELEGRAM_MCP_READ_ONLY", "false")
    assert not load().read_only


def test_write_merges_and_refuses_unknown_keys() -> None:
    write_settings_file({"read_only": True})
    write_settings_file({"auto_sync": False})
    assert read_settings_file() == {"read_only": True, "auto_sync": False}
    with pytest.raises(ValueError, match="data_dir"):
        write_settings_file({"data_dir": "C:/elsewhere"})


def test_core_mode_is_chosen_when_core_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_MCP_CORE_URL", "https://core.test/")
    monkeypatch.setenv("TELEGRAM_MCP_CORE_TOKEN", "t")
    settings = load()
    assert (settings.archive_mode, settings.core_url) == ("core", "https://core.test")


def test_built_in_api_is_used_only_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("telegram_mcp._embedded")
    fake.API_ID = 42  # type: ignore[attr-defined]
    fake.API_HASH = "built-in"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_mcp._embedded", fake)
    settings = load()
    assert (settings.api_id, settings.api_hash.get_secret_value()) == (42, "built-in")

    write_settings_file({"api_id": 7, "api_hash": "own"})
    assert load().api_id == 7


def test_settings_file_follows_the_data_dir_variable(tmp_path: Path) -> None:
    assert config.settings_file() == tmp_path / "settings.json"
