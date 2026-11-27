from typing import Any

from fakes import AZIZ, TELEGRAM_SERVICE, WORK_GROUP, FakeCore, FakeManager, FakeTelegram
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent

from telegram_mcp.config import Settings
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.server import build_server
from telegram_mcp.tools import history

HISTORY_TOOLS = {
    "history_search",
    "history_status",
    "history_sync",
    "history_include_chat",
    "history_exclude_chat",
}


class Harness:
    """The history tools wired to a fake Telegram account and a fake core server."""

    def __init__(self, settings: Settings) -> None:
        self.telegram = FakeTelegram({AZIZ: [1, 2, 3], WORK_GROUP: [1, 2], TELEGRAM_SERVICE: [1]})
        self.core = FakeCore()
        self.server = MCPServer("test")
        history.register(
            self.server,
            FakeManager(self.telegram),  # type: ignore[arg-type]
            settings,
            ChatPolicy(),
            self.core.archive(),
        )

    async def call(self, tool: str, **arguments: Any) -> tuple[bool, str]:
        async with Client(self.server) as client:
            result = await client.call_tool(tool, arguments)
        (block,) = result.content
        assert isinstance(block, TextContent)
        return bool(result.is_error), block.text


async def _tool_names(settings: Settings, core: FakeCore | None) -> set[str]:
    server = build_server(settings, core.archive() if core else None)
    async with Client(server) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


async def test_history_tools_exist_only_when_the_archive_is_configured(
    settings: Settings,
) -> None:
    assert not HISTORY_TOOLS & await _tool_names(settings, None)
    assert await _tool_names(settings, FakeCore()) >= HISTORY_TOOLS


async def test_read_only_mode_keeps_search_but_drops_everything_that_uploads(
    settings: Settings,
) -> None:
    read_only = settings.model_copy(update={"read_only": True})
    names = await _tool_names(read_only, FakeCore())
    assert names & HISTORY_TOOLS == {"history_search", "history_status"}


async def test_sync_then_search_and_status(settings: Settings) -> None:
    harness = Harness(settings)
    is_error, text = await harness.call("history_sync")
    assert not is_error, text
    assert "Checked 1 chats, updated 1: 3 new messages" in text

    is_error, text = await harness.call("history_search", query="message 2")
    assert not is_error, text
    assert "1 messages" in text
    assert "#2 | " in text and f"in Aziz [{AZIZ}]" in text
    assert text.endswith("message 2")

    _, text = await harness.call("history_status")
    assert f"{AZIZ} | user | Aziz | 3 messages" in text
    assert "3 messages in 1 chats" in text


async def test_group_is_archived_only_after_opt_in(settings: Settings) -> None:
    harness = Harness(settings)
    await harness.call("history_sync")
    assert harness.core.ids(WORK_GROUP) == []

    is_error, text = await harness.call("history_include_chat", chat=WORK_GROUP)
    assert not is_error, text
    assert harness.core.selected == [WORK_GROUP]
    await harness.call("history_sync")
    assert harness.core.ids(WORK_GROUP) == [1, 2]
    _, status = await harness.call("history_status")
    assert f"{WORK_GROUP} | supergroup | Work | 2 messages" in status
    (group_line,) = [line for line in status.splitlines() if line.startswith(str(WORK_GROUP))]
    assert group_line.endswith("| opted in")


async def test_personal_chat_needs_no_opt_in(settings: Settings) -> None:
    harness = Harness(settings)
    _, text = await harness.call("history_include_chat", chat=AZIZ)
    assert "always archived" in text
    assert harness.core.selected == []


async def test_exclude_keeps_or_purges_the_stored_copy(settings: Settings) -> None:
    harness = Harness(settings)
    await harness.call("history_include_chat", chat=WORK_GROUP)
    await harness.call("history_sync")

    _, text = await harness.call("history_exclude_chat", chat=WORK_GROUP)
    assert "were kept" in text
    assert (harness.core.selected, harness.core.ids(WORK_GROUP)) == ([], [1, 2])

    _, text = await harness.call("history_exclude_chat", chat=WORK_GROUP, purge=True)
    assert "2 stored messages deleted" in text
    assert harness.core.ids(WORK_GROUP) == []


async def test_personal_chat_cannot_be_silently_excluded(settings: Settings) -> None:
    harness = Harness(settings)
    is_error, text = await harness.call("history_exclude_chat", chat=AZIZ)
    assert is_error
    assert "TELEGRAM_MCP_HIDDEN_CHATS" in text


async def test_the_login_code_chat_cannot_enter_the_archive_by_any_tool(
    settings: Settings,
) -> None:
    harness = Harness(settings)
    harness.core.selected.append(TELEGRAM_SERVICE)  # even if the server says it is opted in
    await harness.call("history_sync")
    assert harness.core.ids(TELEGRAM_SERVICE) == []

    for tool in ("history_include_chat", "history_exclude_chat"):
        is_error, text = await harness.call(tool, chat=TELEGRAM_SERVICE)
        assert is_error and "hidden by server policy" in text
    is_error, text = await harness.call("history_search", query="code", chat=TELEGRAM_SERVICE)
    assert is_error and "hidden by server policy" in text
    is_error, text = await harness.call("history_sync", chat=TELEGRAM_SERVICE)
    assert is_error and "hidden by server policy" in text


async def test_an_unreachable_archive_is_explained_to_the_model(settings: Settings) -> None:
    harness = Harness(settings)
    harness.core.token = "rotated"  # the server no longer accepts our token
    is_error, text = await harness.call("history_status")
    assert is_error
    assert "TELEGRAM_MCP_CORE_TOKEN" in text
