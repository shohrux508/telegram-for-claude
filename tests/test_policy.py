from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent
from telethon import types

from telegram_mcp.accounts import TelegramMCPError, resolve_chat
from telegram_mcp.config import Settings
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.tools import reading

TELEGRAM_SERVICE = 777000
FRIEND = 5


def _dialog(user_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name=name,
        entity=types.User(id=user_id, first_name=name),
        input_entity=types.InputPeerUser(user_id, 0),
        unread_count=3,
        unread_mentions_count=0,
        pinned=False,
        archived=False,
        dialog=SimpleNamespace(notify_settings=None, read_inbox_max_id=0),
        message=None,
    )


class FakeClient:
    """A logged-in account whose dialog list includes Telegram's service chat."""

    dialogs = (_dialog(TELEGRAM_SERVICE, "Telegram"), _dialog(FRIEND, "Aziz"))

    async def iter_dialogs(self, **_: Any) -> AsyncIterator[SimpleNamespace]:
        for dialog in self.dialogs:
            yield dialog

    async def get_messages(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def get_input_entity(self, ref: Any) -> types.InputPeerUser:
        return types.InputPeerUser(int(ref), 0)


class FakeManager:
    async def get(self, account: str | None = None) -> FakeClient:
        return FakeClient()


async def _call(settings: Settings, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    server = MCPServer("test")
    reading.register(server, FakeManager(), settings, ChatPolicy())  # type: ignore[arg-type]
    async with Client(server) as client:
        result = await client.call_tool(tool, arguments)
    (block,) = result.content
    assert isinstance(block, TextContent)
    return bool(result.is_error), block.text


def test_service_chats_are_hidden_and_others_are_not() -> None:
    policy = ChatPolicy()
    with pytest.raises(TelegramMCPError, match="hidden by server policy"):
        policy.ensure_visible(types.InputPeerUser(TELEGRAM_SERVICE, 0))
    policy.ensure_visible(types.InputPeerUser(FRIEND, 0))
    policy.ensure_visible(types.InputPeerSelf())


def test_user_can_hide_more_chats_by_marked_id() -> None:
    policy = ChatPolicy([-1001234567890])
    with pytest.raises(TelegramMCPError):
        policy.ensure_visible(types.InputPeerChannel(1234567890, 0))
    assert policy.is_hidden(TELEGRAM_SERVICE)  # configuration only ever adds to the list


def test_hidden_chats_setting_accepts_comma_separated_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "x")
    monkeypatch.setenv("TELEGRAM_MCP_HIDDEN_CHATS", "-1001234567890, 42")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.hidden_chats == [-1001234567890, 42]


async def test_resolving_the_service_chat_is_refused_however_it_is_named() -> None:
    client = FakeClient()
    for reference in (TELEGRAM_SERVICE, str(TELEGRAM_SERVICE), "+777000"):
        with pytest.raises(TelegramMCPError, match="hidden by server policy"):
            await resolve_chat(client, reference, ChatPolicy())


async def test_chat_list_and_unread_leave_the_service_chat_out(settings: Settings) -> None:
    for tool in ("list_chats", "get_unread"):
        is_error, text = await _call(settings, tool, {})
        assert not is_error, text
        assert "Aziz" in text
        assert str(TELEGRAM_SERVICE) not in text
        assert "Telegram" not in text


async def test_reading_the_service_chat_through_a_tool_is_an_error(settings: Settings) -> None:
    is_error, text = await _call(settings, "get_messages", {"chat": TELEGRAM_SERVICE})
    assert is_error
    assert "hidden by server policy" in text
