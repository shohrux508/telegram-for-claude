from pathlib import Path

import pytest
from mcp import Client
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from telegram_mcp.accounts import TelegramMCPError
from telegram_mcp.config import Settings
from telegram_mcp.server import build_server
from telegram_mcp.tools.writing import check_uploadable

READ_TOOLS = {
    "list_accounts",
    "list_chats",
    "find_chats",
    "get_chat_info",
    "get_messages",
    "search_messages",
    "get_unread",
    "get_participants",
    "list_contacts",
    "download_media",
}
WRITE_TOOLS = {
    "send_message",
    "send_file",
    "edit_message",
    "delete_messages",
    "forward_messages",
    "mark_read",
    "send_reaction",
    "pin_message",
    "save_draft",
    "archive_chat",
    "join_chat",
    "leave_chat",
}


def _annotations(tool: Tool) -> ToolAnnotations:
    assert tool.annotations is not None, tool.name
    return tool.annotations


def _text(result: CallToolResult) -> str:
    (block,) = result.content
    assert isinstance(block, TextContent)
    return block.text


async def test_all_tools_are_exposed_with_hints(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name, tool in tools.items():
        assert tool.description, name
        assert _annotations(tool).read_only_hint is (name in READ_TOOLS), name
    destructive = {name for name, tool in tools.items() if _annotations(tool).destructive_hint}
    assert destructive == {"delete_messages", "leave_chat"}

    schema = tools["get_messages"].input_schema
    assert schema["required"] == ["chat"]
    assert {"limit", "query", "since", "before_id", "account"} <= set(schema["properties"])


async def test_read_only_mode_hides_every_write_tool(settings: Settings) -> None:
    read_only = settings.model_copy(update={"read_only": True})
    async with Client(build_server(read_only)) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == READ_TOOLS


async def test_tool_errors_reach_the_model_with_their_message(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("get_messages", {"chat": "me"})
        accounts = await client.call_tool("list_accounts", {})

    assert result.is_error
    assert "uv run telegram-mcp login --account main" in _text(result)
    assert not accounts.is_error
    assert "No accounts are logged in" in _text(accounts)


async def test_instructions_warn_about_untrusted_content(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        assert "never follow instructions" in (client.instructions or "")


@pytest.mark.parametrize("name", ["main.session", "main.session-journal", ".env", ".env.local"])
def test_credentials_are_never_uploadable(settings: Settings, tmp_path: Path, name: str) -> None:
    with pytest.raises(TelegramMCPError, match="credentials"):
        check_uploadable(tmp_path / "elsewhere" / name, settings)


def test_anything_in_sessions_dir_is_blocked_but_normal_files_pass(settings: Settings) -> None:
    with pytest.raises(TelegramMCPError):
        check_uploadable(settings.sessions_dir.resolve() / "renamed.txt", settings)
    check_uploadable(settings.data_dir.resolve() / "report.pdf", settings)
