"""Tools for the history archive: kept on this computer or on the core server.

Registered only when the archive is configured. "History archive" here is the
searchable copy of messages (see archive.Archive); it is unrelated to Telegram's Archive
folder, which the `archive_chat` tool manages.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from telethon import utils

from telegram_mcp.accounts import AccountManager, TelegramMCPError, resolve_chat
from telegram_mcp.archive import Archive
from telegram_mcp.config import Settings
from telegram_mcp.formatting import (
    display_name,
    entity_kind,
    format_datetime,
    local_timezone_label,
    parse_datetime,
)
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.sync import ALWAYS_ARCHIVED_KIND, sync_account
from telegram_mcp.tools import DESTRUCTIVE, READ, WRITE, telegram_tool

_MAX_SYNC_SECONDS = 50


def _when(value: str | None) -> str:
    return format_datetime(datetime.fromisoformat(value)) if value else "?"


def format_hit(hit: dict[str, Any]) -> str:
    sender = "me" if hit["is_outgoing"] else hit["sender_name"] or f"[{hit['sender_id']}]"
    header = [
        f"#{hit['message_id']}",
        _when(hit["sent_at"]),
        f"in {hit['chat_title'] or '?'} [{hit['chat_id']}]",
        sender,
    ]
    if hit["reply_to_id"]:
        header.append(f"reply to #{hit['reply_to_id']}")
    if hit["media"]:
        header.append(f"[{hit['media']}]")
    line = " | ".join(header)
    return f"{line}\n{hit['text']}" if hit["text"] else line


def register(
    server: MCPServer,
    manager: AccountManager,
    settings: Settings,
    policy: ChatPolicy,
    archive: Archive,
) -> None:
    async def account_id_of(client: Any) -> int:
        return int((await client.get_me()).id)

    async def chat_id_of(client: Any, chat: str | int) -> int:
        return int(await client.get_peer_id(await resolve_chat(client, chat, policy)))

    @telegram_tool(server, READ)
    async def history_search(
        query: str = "",
        chat: str | int | None = None,
        from_user: str | int | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        sort: Literal["relevance", "date"] = "relevance",
        account: str | None = None,
    ) -> str:
        """Search the history archive: fast full-text search over all archived chats at once.

        Prefer this over search_messages for finding old conversations: it understands
        word forms (Russian endings, Uzbek suffixes) and ё/е. Bare words must all be
        present; "quoted text" is an exact phrase. With an empty query it lists the
        newest archived messages matching the filters. Only archived chats are covered,
        see history_status. To read around a hit, call get_messages on that chat with
        before_id / after_id.
        Args:
            chat: only this chat (numeric id, @username, or exact title).
            from_user: only messages sent by this user.
            since / until: ISO date or datetime bounds, local time unless an offset is given.
            limit: how many messages to return (max 100).
            sort: "relevance" (best match first) or "date" (newest first).
        """
        client = await manager.get(account)
        hits = await archive.search(
            q=query,
            account_id=await account_id_of(client),
            chat_id=await chat_id_of(client, chat) if chat is not None else None,
            sender_id=await chat_id_of(client, from_user) if from_user is not None else None,
            since=parse_datetime(since).isoformat() if since else None,
            until=parse_datetime(until).isoformat() if until else None,
            limit=max(1, min(limit, 100)),
            sort=sort,
        )
        if not hits:
            return "Nothing found in the history archive. Check history_status for coverage."
        header = f"{len(hits)} messages (times in {local_timezone_label()}):"
        return "\n\n".join([header, *(format_hit(hit) for hit in hits)])

    @telegram_tool(server, READ)
    async def history_status(account: str | None = None) -> str:
        """Show what the history archive holds: every archived chat with its message count.

        Personal chats are archived automatically; groups, channels and bots only after
        history_include_chat ("opted in").
        """
        client = await manager.get(account)
        chats = await archive.chats(await account_id_of(client))
        if not chats:
            return "The history archive is empty. Run history_sync to fill it."
        total = sum(chat["messages"] for chat in chats)
        lines = [f"{total} messages in {len(chats)} chats (times in {local_timezone_label()}):"]
        for chat in chats:
            flag = " | opted in" if chat["selected"] else ""
            lines.append(
                f"{chat['chat_id']} | {chat['kind']} | {chat['title']} | "
                f"{chat['messages']} messages | {_when(chat['oldest_at'])} .. "
                f"{_when(chat['newest_at'])}{flag}"
            )
        return "\n".join(lines)

    if settings.read_only:
        return

    @telegram_tool(server, WRITE)
    async def history_sync(
        max_seconds: int = 30,
        backfill: int = 0,
        chat: str | int | None = None,
        account: str | None = None,
    ) -> str:
        """Copy new messages from Telegram into the history archive (on this computer or server).

        Covers personal chats and opted-in chats. Runs for at most `max_seconds`; if it
        reports "Not finished", call it again. Nothing changes in Telegram itself.
        Args:
            max_seconds: time budget for this call (max 50).
            backfill: also fetch this many older messages per chat, below what is archived.
            chat: only this chat.
        """
        client = await manager.get(account)
        report = await sync_account(
            client,
            archive,
            policy,
            initial_limit=settings.sync_initial_limit,
            backfill=max(0, backfill),
            only_chat_id=await chat_id_of(client, chat) if chat is not None else None,
            max_seconds=max(1, min(max_seconds, _MAX_SYNC_SECONDS)),
            max_flood_wait=10,
        )
        return report.summary()

    @telegram_tool(server, WRITE)
    async def history_include_chat(chat: str | int, account: str | None = None) -> str:
        """Opt a group, channel or bot chat into the history archive.

        From the next sync on, its messages are copied into the archive.
        Do this only for chats the user named. Personal chats need no opt-in.
        """
        client = await manager.get(account)
        entity = await client.get_entity(await resolve_chat(client, chat, policy))
        kind = entity_kind(entity)
        if kind == ALWAYS_ARCHIVED_KIND:
            return f"{display_name(entity)} is a personal chat: those are always archived."
        chat_id = utils.get_peer_id(entity)
        await archive.set_selection(
            await account_id_of(client),
            chat_id,
            selected=True,
            title=display_name(entity),
            kind=kind,
        )
        return f"{display_name(entity)} [{chat_id}] will be archived from the next history_sync."

    @telegram_tool(server, DESTRUCTIVE)
    async def history_exclude_chat(
        chat: str | int, purge: bool = False, account: str | None = None
    ) -> str:
        """Stop archiving an opted-in chat; with `purge`, also delete its archived copy.

        Purging cannot be undone except by archiving the chat again. Telegram itself is
        not touched.
        Args:
            purge: delete the messages of this chat already stored in the archive.
        """
        client = await manager.get(account)
        entity = await client.get_entity(await resolve_chat(client, chat, policy))
        if entity_kind(entity) == ALWAYS_ARCHIVED_KIND and not purge:
            raise TelegramMCPError(
                "Personal chats are always archived. To keep one out entirely, the user must "
                "add its id to TELEGRAM_MCP_HIDDEN_CHATS, which also hides it from Claude."
            )
        chat_id = utils.get_peer_id(entity)
        purged = await archive.set_selection(
            await account_id_of(client), chat_id, selected=False, purge=purge
        )
        name = f"{display_name(entity)} [{chat_id}]"
        if purge and entity_kind(entity) == ALWAYS_ARCHIVED_KIND:
            return (
                f"{name}: {purged} stored messages deleted. It is a personal chat, so the next "
                "sync archives it again unless its id is added to TELEGRAM_MCP_HIDDEN_CHATS."
            )
        if purge:
            return f"{name}: removed from the archive, {purged} stored messages deleted."
        return f"{name}: no longer archived; messages already stored were kept."
