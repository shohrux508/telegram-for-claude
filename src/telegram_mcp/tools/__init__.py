"""MCP tool definitions, split by whether they change anything in Telegram."""

from __future__ import annotations

import functools
import sqlite3
from collections.abc import Awaitable, Callable
from typing import ParamSpec

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from telethon.errors import FloodWaitError, RPCError

from telegram_mcp.accounts import TelegramMCPError

P = ParamSpec("P")

ToolFn = Callable[P, Awaitable[str]]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


def telegram_tool(
    server: MCPServer, annotations: ToolAnnotations
) -> Callable[[ToolFn[P]], ToolFn[P]]:
    """Register a coroutine as an MCP tool.

    The SDK hides the text of unexpected exceptions from the model, so the
    failures we do expect are converted to ToolError with a readable message.
    """

    def decorator(fn: ToolFn[P]) -> ToolFn[P]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
            try:
                return await fn(*args, **kwargs)
            except FloodWaitError as exc:
                raise ToolError(
                    f"Telegram rate limit: retry in {exc.seconds} seconds, do not retry sooner."
                ) from exc
            except RPCError as exc:
                raise ToolError(f"Telegram API error: {exc}") from exc
            except sqlite3.OperationalError as exc:
                raise ToolError(
                    f"Session database error ({exc}). Another telegram-mcp process is probably "
                    "using the same account: close the other Claude app or session."
                ) from exc
            except (TelegramMCPError, ValueError, ConnectionError) as exc:
                raise ToolError(str(exc)) from exc

        # Plain text only: structured output would send every result twice.
        server.tool(annotations=annotations, structured_output=False)(wrapper)
        return fn

    return decorator
