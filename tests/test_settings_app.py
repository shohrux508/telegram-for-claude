"""The settings page server, driven over HTTP the way the browser page drives it."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fakes import AZIZ, SHOP_BOT, TELEGRAM_SERVICE, WORK_GROUP, FakeTelegram

from telegram_mcp.app import claude_config
from telegram_mcp.app.web import SettingsApp, build_app
from telegram_mcp.config import Settings, read_settings_file

PORT = 54321


class FakeManager:
    def __init__(self, client: FakeTelegram | None) -> None:
        self.client = client

    def list_accounts(self) -> list[str]:
        return ["main"] if self.client else []

    async def get(self, account: str | None = None) -> FakeTelegram:
        assert self.client is not None
        return self.client

    async def close(self) -> None:
        pass


def load() -> Settings:
    return Settings(api_id=1, api_hash="h", _env_file=None)  # type: ignore[call-arg, arg-type]


@pytest.fixture
def no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Claude Desktop at a temp config and pretend Claude Code is not installed."""
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(claude_config, "desktop_config_paths", lambda: [config])
    monkeypatch.setattr(claude_config, "find_claude_code", lambda: None)
    return config


@pytest.fixture
async def page(no_claude: Path) -> AsyncIterator[tuple[SettingsApp, httpx2.AsyncClient]]:
    app = SettingsApp(load, PORT)
    app.manager = FakeManager(None)  # type: ignore[assignment]
    transport = httpx2.ASGITransport(app=build_app(app))
    async with httpx2.AsyncClient(
        transport=transport,
        base_url=f"http://127.0.0.1:{PORT}",
        headers={"X-Token": app.token},
    ) as client:
        yield app, client


async def test_requests_without_the_token_or_from_another_host_are_refused(
    page: tuple[SettingsApp, httpx2.AsyncClient],
) -> None:
    app, client = page
    assert (await client.get("/api/state", headers={"X-Token": "wrong"})).status_code == 403
    # DNS rebinding: a page on evil.example resolving to 127.0.0.1 still sends its own Host.
    rebound = await client.get("/api/state", headers={"Host": "evil.example"})
    assert rebound.status_code == 403
    assert (await client.get("/", headers={"X-Token": ""})).status_code == 403
    page_html = await client.get(f"/?t={app.token}", headers={"X-Token": ""})
    assert page_html.status_code == 200
    assert app.token in page_html.text and "__TOKEN__" not in page_html.text


async def test_state_of_a_fresh_install(page: tuple[SettingsApp, httpx2.AsyncClient]) -> None:
    _, client = page
    state = (await client.get("/api/state")).json()
    assert state["accounts"] == []
    assert state["api_configured"]
    assert state["settings"]["archive_mode"] == "local"
    assert state["settings"]["core_token"] is False  # secrets are reported as set/unset
    assert state["claude"] == {
        "desktop": [
            {"path": state["claude"]["desktop"][0]["path"], "exists": False, "registered": False}
        ],
        "code": None,
    }


async def test_settings_are_saved_to_the_file_and_validated(
    page: tuple[SettingsApp, httpx2.AsyncClient],
) -> None:
    app, client = page
    assert (await client.post("/api/settings", json={"read_only": True})).status_code == 200
    assert read_settings_file()["read_only"] is True
    assert app.settings.read_only

    bad = await client.post("/api/settings", json={"sync_interval_minutes": 0})
    assert bad.status_code == 400
    assert "sync_interval_minutes" not in read_settings_file()

    # Keys the page may not touch are dropped, a blank secret keeps the saved one.
    await client.post("/api/settings", json={"core_token": "t1", "data_dir": "C:/x"})
    await client.post("/api/settings", json={"core_token": ""})
    assert read_settings_file() == {"read_only": True, "core_token": "t1"}


async def test_connect_writes_claude_desktop_config_keeping_other_servers(
    page: tuple[SettingsApp, httpx2.AsyncClient], no_claude: Path
) -> None:
    _, client = page
    no_claude.parent.mkdir(parents=True)
    no_claude.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))

    result = (await client.post("/api/claude/connect", json={})).json()
    assert result == {"desktop": [str(no_claude)], "code": False, "code_error": ""}
    data = json.loads(no_claude.read_text())
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "x"}
    entry = data["mcpServers"]["telegram"]
    assert entry["args"][-1] == "serve"
    assert no_claude.with_suffix(".json.bak").exists()

    state = (await client.get("/api/state")).json()
    assert state["claude"]["desktop"][0]["registered"] is True


async def test_chat_list_hides_service_chats_and_saves_choices(
    page: tuple[SettingsApp, httpx2.AsyncClient],
) -> None:
    app, client = page
    telegram = FakeTelegram({AZIZ: [1], WORK_GROUP: [1], SHOP_BOT: [1], TELEGRAM_SERVICE: [1]})
    app.manager = FakeManager(telegram)  # type: ignore[assignment]

    rows = {c["id"]: c for c in (await client.get("/api/chats")).json()["chats"]}
    assert TELEGRAM_SERVICE not in rows
    assert rows[AZIZ]["always_archived"] and rows[AZIZ]["archived"]
    assert not rows[WORK_GROUP]["archived"]

    saved = await client.post("/api/chats", json={"hidden": [SHOP_BOT], "archive": [WORK_GROUP]})
    assert saved.status_code == 200, saved.text
    assert read_settings_file()["hidden_chats"] == [SHOP_BOT]

    rows = {c["id"]: c for c in (await client.get("/api/chats")).json()["chats"]}
    assert rows[SHOP_BOT]["hidden"] and rows[WORK_GROUP]["archived"]


async def test_login_without_api_id_explains_itself(
    page: tuple[SettingsApp, httpx2.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client = page

    def no_api() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    app._load = no_api
    await app.reload()
    state: dict[str, Any] = (await client.post("/api/login/start", json={})).json()
    assert state["status"] == "error"
    assert "API ID" in state["error"]
    assert not (await client.get("/api/state")).json()["api_configured"]


async def test_quit_stops_the_app(page: tuple[SettingsApp, httpx2.AsyncClient]) -> None:
    app, client = page
    await client.post("/api/quit", json={})
    assert app.stop.is_set()


def test_server_command_runs_serve() -> None:
    assert claude_config.server_command()[-1] == "serve"
