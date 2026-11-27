"""Tools that only read from Telegram."""

from __future__ import annotations

import re
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from telethon import functions, types, utils
from telethon.errors import RPCError

from telegram_mcp.accounts import AccountManager, TelegramMCPError, resolve_chat
from telegram_mcp.config import Settings
from telegram_mcp.formatting import (
    display_name,
    entity_kind,
    format_datetime,
    format_dialog,
    format_entity,
    format_messages,
    format_size,
    is_muted,
    local_timezone_label,
    parse_datetime,
)
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.tools import READ, telegram_tool

MAX_MESSAGES = 500
MAX_CHATS = 500
# How many dialogs get_unread walks through looking for unread ones.
_UNREAD_SCAN_LIMIT = 400
_SAFE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}")

ChatKind = Literal["user", "bot", "group", "channel"]


def _clamp(value: int, maximum: int) -> int:
    return max(1, min(value, maximum))


def _matches_kind(entity: Any, kind: ChatKind) -> bool:
    actual = entity_kind(entity)
    if kind == "group":
        return actual in ("group", "supergroup", "forum")
    return actual == kind


def _role(user: Any) -> str | None:
    name = type(getattr(user, "participant", None)).__name__
    if "Creator" in name:
        return "creator"
    if "Admin" in name:
        return "admin"
    return None


def _user_status(user: Any) -> str:
    status = user.status
    if status is None:
        return "hidden"
    label = type(status).__name__.removeprefix("UserStatus").lower()
    was_online = getattr(status, "was_online", None)
    return f"{label}, last seen {format_datetime(was_online)}" if was_online else label


async def _chat_title(client: Any, peer: Any, messages: list[Any]) -> str:
    chat_id = await client.get_peer_id(peer)
    chat = messages[0].chat if messages else None
    return f"{display_name(chat)} [{chat_id}]" if chat is not None else f"[{chat_id}]"


def register(
    server: MCPServer, manager: AccountManager, settings: Settings, policy: ChatPolicy
) -> None:
    @telegram_tool(server, READ)
    async def list_accounts() -> str:
        """List the Telegram accounts logged in on this machine and who they belong to.

        The names shown here are what other tools accept as `account`.
        """
        names = manager.list_accounts()
        if not names:
            return (
                "No accounts are logged in. The user must run `uv run telegram-mcp login` "
                "in a terminal in the project folder."
            )
        lines = []
        for name in names:
            try:
                client = await manager.get(name)
                me = await client.get_me()
                lines.append(f"{name}: {format_entity(me)}")
            except (TelegramMCPError, RPCError, ConnectionError) as exc:
                lines.append(f"{name}: unavailable ({exc})")
        lines.append(f"Used when `account` is omitted: {manager.resolve_account_name(None)}")
        return "\n".join(lines)

    @telegram_tool(server, READ)
    async def list_chats(
        limit: int = 50,
        kind: ChatKind | None = None,
        unread_only: bool = False,
        archived: bool | None = None,
        query: str | None = None,
        account: str | None = None,
    ) -> str:
        """List the account's chats (dialogs), most recently active first.

        Each line: `id | kind | name | @username | unread | flags | last message`.
        Args:
            limit: how many chats to return (max 500).
            kind: only "user", "bot", "group" (incl. supergroups and forums) or "channel".
            unread_only: only chats with unread messages.
            archived: true for archived only, false for non-archived only, omit for both.
            query: case-insensitive substring of the chat name or username.
        """
        client = await manager.get(account)
        limit = _clamp(limit, MAX_CHATS)
        needle = query.casefold().lstrip("@") if query else None
        lines: list[str] = []
        async for dialog in client.iter_dialogs(archived=archived, ignore_migrated=True):
            if policy.is_hidden(dialog.id):
                continue
            if kind and not _matches_kind(dialog.entity, kind):
                continue
            if unread_only and not dialog.unread_count:
                continue
            if needle:
                username = getattr(dialog.entity, "username", None) or ""
                if needle not in (dialog.name or "").casefold() and needle not in username.lower():
                    continue
            lines.append(format_dialog(dialog))
            if len(lines) >= limit:
                break
        if not lines:
            return "No chats match."
        return f"{len(lines)} chats (times in {local_timezone_label()}):\n" + "\n".join(lines)

    @telegram_tool(server, READ)
    async def find_chats(query: str, limit: int = 20, account: str | None = None) -> str:
        """Search Telegram for users, bots, groups and channels by name or username.

        Unlike list_chats this also finds public chats the account has never opened.
        """
        client = await manager.get(account)
        result = await client(functions.contacts.SearchRequest(q=query, limit=_clamp(limit, 100)))
        entities = {
            pid: e
            for e in [*result.users, *result.chats]
            if not policy.is_hidden(pid := utils.get_peer_id(e))
        }

        def section(title: str, peers: list[Any]) -> list[str]:
            found = [entities[pid] for p in peers if (pid := utils.get_peer_id(p)) in entities]
            return [title, *(format_entity(e) for e in found)] if found else []

        lines = section("Among your chats and contacts:", result.my_results)
        lines += section("Global search:", result.results)
        return "\n".join(lines) if lines else f"Nothing found for {query!r}."

    @telegram_tool(server, READ)
    async def get_chat_info(chat: str | int, account: str | None = None) -> str:
        """Get details of a user, bot, group or channel: description, member count, status.

        Args:
            chat: numeric id, @username, t.me link, +phone, "me", or exact chat title.
        """
        client = await manager.get(account)
        entity = await client.get_entity(await resolve_chat(client, chat, policy))
        lines = [format_entity(entity)]

        if isinstance(entity, types.User):
            full = (await client(functions.users.GetFullUserRequest(entity))).full_user
            if entity.phone:
                lines.append(f"phone: +{entity.phone}")
            lines.append(f"status: {_user_status(entity)}")
            flags = [
                name
                for name, enabled in (
                    ("contact", entity.contact),
                    ("mutual contact", entity.mutual_contact),
                    ("verified", entity.verified),
                    ("premium", entity.premium),
                    ("blocked by you", full.blocked),
                    ("scam", entity.scam),
                    ("deleted", entity.deleted),
                )
                if enabled
            ]
            if flags:
                lines.append(f"flags: {', '.join(flags)}")
            if full.about:
                lines.append(f"bio: {full.about}")
            lines.append(f"groups in common: {full.common_chats_count}")
        elif isinstance(entity, types.Channel):
            full = (await client(functions.channels.GetFullChannelRequest(entity))).full_chat
            lines.append(f"members: {full.participants_count}")
            if full.about:
                lines.append(f"description: {full.about}")
            if full.linked_chat_id:
                lines.append(
                    f"linked chat id: {utils.get_peer_id(types.PeerChannel(full.linked_chat_id))}"
                )
            if full.pinned_msg_id:
                lines.append(f"pinned message: #{full.pinned_msg_id}")
            role = "creator" if entity.creator else "admin" if entity.admin_rights else "member"
            lines.append(f"your role: {'not a member' if entity.left else role}")
        elif isinstance(entity, types.Chat):
            full = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
            lines.append(f"members: {entity.participants_count}")
            if full.about:
                lines.append(f"description: {full.about}")
            if full.pinned_msg_id:
                lines.append(f"pinned message: #{full.pinned_msg_id}")
        return "\n".join(lines)

    @telegram_tool(server, READ)
    async def get_messages(
        chat: str | int,
        limit: int = 50,
        query: str | None = None,
        from_user: str | int | None = None,
        since: str | None = None,
        until: str | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
        from_oldest: bool = False,
        topic_id: int | None = None,
        account: str | None = None,
    ) -> str:
        """Read or search the message history of one chat. Does not mark anything as read.

        Returns the newest matching messages by default, printed oldest first. Each message
        is `#id | time | sender | flags` followed by its text. To page further back, call
        again with `before_id` set to the smallest id you received.
        Args:
            chat: numeric id, @username, t.me link, +phone, "me", or exact chat title.
            limit: how many messages to return (max 500).
            query: only messages containing this text (server-side search).
            from_user: only messages sent by this user.
            since / until: ISO date or datetime bounds, local time unless an offset is given.
            before_id / after_id: only messages with an id below / above this one.
            from_oldest: take messages from the start of the range instead of the end.
            topic_id: in a forum, only the topic whose root message has this id.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        since_dt = parse_datetime(since) if since else None
        until_dt = parse_datetime(until) if until else None
        sender = await resolve_chat(client, from_user, policy) if from_user is not None else None

        messages: list[Any] = []
        async for message in client.iter_messages(
            peer,
            limit=_clamp(limit, MAX_MESSAGES),
            search=query,
            from_user=sender,
            offset_date=since_dt if from_oldest else until_dt,
            min_id=after_id or 0,
            max_id=before_id or 0,
            reverse=from_oldest,
            reply_to=topic_id,
        ):
            if from_oldest and until_dt and message.date > until_dt:
                break
            if not from_oldest and since_dt and message.date < since_dt:
                break
            messages.append(message)
        if not from_oldest:
            messages.reverse()

        title = await _chat_title(client, peer, messages)
        if not messages:
            return f"No messages match in {title}."
        header = f"{len(messages)} messages from {title} (times in {local_timezone_label()}):"
        return f"{header}\n\n{format_messages(messages)}"

    @telegram_tool(server, READ)
    async def search_messages(
        query: str,
        limit: int = 30,
        since: str | None = None,
        until: str | None = None,
        account: str | None = None,
    ) -> str:
        """Search message text across all chats of the account at once.

        To search inside one chat use get_messages with `query` instead.
        Args:
            limit: how many messages to return (max 500).
            since / until: ISO date or datetime bounds, local time unless an offset is given.
        """
        client = await manager.get(account)
        since_dt = parse_datetime(since) if since else None
        messages: list[Any] = []
        async for message in client.iter_messages(
            None,
            search=query,
            limit=_clamp(limit, MAX_MESSAGES),
            offset_date=parse_datetime(until) if until else None,
        ):
            if since_dt and message.date < since_dt:
                break
            if not policy.is_hidden(message.chat_id):
                messages.append(message)
        if not messages:
            return f"No messages found for {query!r}."
        messages.reverse()
        header = f"{len(messages)} messages (times in {local_timezone_label()}):"
        return f"{header}\n\n{format_messages(messages, with_chat=True)}"

    @telegram_tool(server, READ)
    async def get_unread(
        max_chats: int = 20,
        per_chat: int = 20,
        include_muted: bool = False,
        account: str | None = None,
    ) -> str:
        """Show unread incoming messages grouped by chat. Does not mark anything as read.

        Args:
            max_chats: how many chats with unread messages to include (max 100).
            per_chat: newest unread messages to show per chat (max 100).
            include_muted: also include chats whose notifications are muted.
        """
        client = await manager.get(account)
        max_chats = _clamp(max_chats, 100)
        per_chat = _clamp(per_chat, 100)
        sections: list[str] = []
        muted_skipped = 0
        not_shown = 0
        async for dialog in client.iter_dialogs(limit=_UNREAD_SCAN_LIMIT, ignore_migrated=True):
            if not dialog.unread_count or policy.is_hidden(dialog.id):
                continue
            if not include_muted and is_muted(dialog):
                muted_skipped += 1
                continue
            if len(sections) >= max_chats:
                not_shown += 1
                continue
            messages = await client.get_messages(
                dialog.input_entity, limit=per_chat, min_id=dialog.dialog.read_inbox_max_id
            )
            incoming = [m for m in reversed(messages) if not m.out]
            title = f"== {format_entity(dialog.entity)} | unread {dialog.unread_count}"
            if dialog.unread_count > len(incoming):
                title += f", showing newest {len(incoming)}"
            sections.append(f"{title}\n{format_messages(incoming)}")

        if not sections:
            summary = "No unread messages"
        else:
            summary = f"{len(sections)} chats with unread messages"
        notes = [f"times in {local_timezone_label()}"]
        if muted_skipped:
            notes.append(f"{muted_skipped} muted chats with unread skipped")
        if not_shown:
            notes.append(f"{not_shown} more chats not shown")
        return f"{summary} ({'; '.join(notes)})" + "".join(f"\n\n{s}" for s in sections)

    @telegram_tool(server, READ)
    async def get_participants(
        chat: str | int, limit: int = 100, query: str | None = None, account: str | None = None
    ) -> str:
        """List members of a group or channel (channels require admin rights).

        Args:
            limit: how many members to return (max 500).
            query: only members whose name or username matches.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        users = await client.get_participants(peer, limit=_clamp(limit, 500), search=query or "")
        if not users:
            return "No members found."
        lines = []
        for user in users:
            role = _role(user)
            lines.append(format_entity(user) + (f" | {role}" if role else ""))
        return f"{len(users)} of {users.total} members:\n" + "\n".join(lines)

    @telegram_tool(server, READ)
    async def list_contacts(query: str | None = None, account: str | None = None) -> str:
        """List the account's saved contacts with phone numbers.

        Args:
            query: case-insensitive substring of the name, username or phone.
        """
        client = await manager.get(account)
        result = await client(functions.contacts.GetContactsRequest(hash=0))
        needle = query.casefold().lstrip("@+") if query else None
        lines = []
        for user in result.users:
            line = format_entity(user) + (f" | +{user.phone}" if user.phone else "")
            if needle is None or needle in line.casefold():
                lines.append(line)
        if not lines:
            return "No contacts match."
        return f"{len(lines)} contacts:\n" + "\n".join(sorted(lines, key=str.casefold))

    @telegram_tool(server, READ)
    async def download_media(
        chat: str | int, message_id: int, max_mb: int = 50, account: str | None = None
    ) -> str:
        """Download the photo, file, voice or video attached to a message to local disk.

        Returns the saved path; open it with your file tools to look at the content.
        Args:
            max_mb: refuse files larger than this many megabytes.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        message = await client.get_messages(peer, ids=message_id)
        if message is None:
            raise TelegramMCPError(f"Message #{message_id} does not exist in this chat.")
        file = message.file
        if file is None:
            raise TelegramMCPError(f"Message #{message_id} has no downloadable media.")
        if (file.size or 0) > max_mb * 1024 * 1024:
            raise TelegramMCPError(
                f"File is {format_size(file.size)}, above the {max_mb} MB limit. "
                "Raise `max_mb` to download it anyway."
            )

        chat_id = await client.get_peer_id(peer)
        account_name = manager.resolve_account_name(account)
        target_dir = settings.downloads_dir / account_name / str(chat_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        # The name is built here, never taken from the sender-controlled file name.
        extension = file.ext if file.ext and _SAFE_EXTENSION.fullmatch(file.ext) else ""
        target = target_dir / f"{message_id}{extension}"
        if not target.exists():
            await message.download_media(file=str(target))
        original = f", original name {file.name!r}" if file.name else ""
        return f"Saved to {target} ({format_size(file.size)}{original})"
