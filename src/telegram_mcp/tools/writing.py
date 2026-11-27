"""Tools that change something in Telegram. Not registered in read-only mode."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from telethon import functions, types, utils

from telegram_mcp.accounts import AccountManager, TelegramMCPError, resolve_chat
from telegram_mcp.config import Settings
from telegram_mcp.formatting import format_datetime, format_entity, parse_datetime
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.tools import DESTRUCTIVE, WRITE, telegram_tool

Formatting = Literal["markdown", "html", "plain"]

_PARSE_MODES: dict[str, str | None] = {"markdown": "md", "html": "html", "plain": None}
_SECRET_SUFFIXES = (".session", ".session-journal")


def check_uploadable(path: Path, settings: Settings) -> None:
    """Refuse to upload files that hold this server's own credentials.

    A leaked session file is a full account takeover, so this holds even if the
    model was talked into sending it.
    """
    sessions_dir = settings.sessions_dir.resolve()
    if (
        path.suffix in _SECRET_SUFFIXES
        or path.name == ".env"
        or path.name.startswith(".env.")
        or path.is_relative_to(sessions_dir)
    ):
        raise TelegramMCPError(f"Refusing to upload {path.name}: it may contain credentials.")


def _resolve_upload(file_path: str, settings: Settings) -> Path:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        raise TelegramMCPError(f"File not found: {path}")
    check_uploadable(path, settings)
    return path


def register(
    server: MCPServer, manager: AccountManager, settings: Settings, policy: ChatPolicy
) -> None:
    @telegram_tool(server, WRITE)
    async def send_message(
        chat: str | int,
        text: str,
        reply_to: int | None = None,
        formatting: Formatting = "markdown",
        schedule: str | None = None,
        silent: bool = False,
        link_preview: bool = True,
        account: str | None = None,
    ) -> str:
        """Send a text message from the user's account. The recipient sees it as the user.

        Args:
            chat: numeric id, @username, t.me link, +phone, "me", or exact chat title.
            reply_to: id of the message to reply to (in a forum: the topic id).
            formatting: "markdown" (**bold**, __italic__, `code`, [text](url)), "html" or "plain".
            schedule: ISO datetime to send later instead of now (local time unless offset given).
            silent: deliver without a notification sound.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        when = parse_datetime(schedule) if schedule else None
        message = await client.send_message(
            peer,
            text,
            reply_to=reply_to,
            parse_mode=_PARSE_MODES[formatting],
            schedule=when,
            silent=silent,
            link_preview=link_preview,
        )
        chat_id = await client.get_peer_id(peer)
        if when:
            return f"Scheduled message #{message.id} in chat {chat_id} for {format_datetime(when)}."
        return f"Sent message #{message.id} to chat {chat_id}."

    @telegram_tool(server, WRITE)
    async def send_file(
        chat: str | int,
        file_path: str,
        caption: str | None = None,
        reply_to: int | None = None,
        as_document: bool = False,
        voice_note: bool = False,
        account: str | None = None,
    ) -> str:
        """Send a local file (photo, document, audio, video) from the user's account.

        Args:
            file_path: absolute path of the file on this machine.
            caption: optional text shown with the file (markdown).
            as_document: send images and videos uncompressed, as files.
            voice_note: send an audio file as a voice message.
        """
        path = await asyncio.to_thread(_resolve_upload, file_path, settings)
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        message = await client.send_file(
            peer,
            str(path),
            caption=caption,
            reply_to=reply_to,
            force_document=as_document,
            voice_note=voice_note,
        )
        return (
            f"Sent {path.name} as message #{message.id} to chat {await client.get_peer_id(peer)}."
        )

    @telegram_tool(server, WRITE)
    async def edit_message(
        chat: str | int,
        message_id: int,
        text: str,
        formatting: Formatting = "markdown",
        account: str | None = None,
    ) -> str:
        """Replace the text of a message the user sent earlier."""
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        await client.edit_message(peer, message_id, text, parse_mode=_PARSE_MODES[formatting])
        return f"Edited message #{message_id}."

    @telegram_tool(server, DESTRUCTIVE)
    async def delete_messages(
        chat: str | int,
        message_ids: list[int],
        for_everyone: bool = True,
        account: str | None = None,
    ) -> str:
        """Permanently delete messages. This cannot be undone.

        Args:
            for_everyone: also delete for the other side; false removes them only for the user
                (private chats and basic groups; in supergroups and channels it is always everyone).
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        results = await client.delete_messages(peer, message_ids, revoke=for_everyone)
        deleted = sum(getattr(result, "pts_count", 0) for result in results)
        return f"Deleted {deleted} of {len(message_ids)} messages."

    @telegram_tool(server, WRITE)
    async def forward_messages(
        from_chat: str | int,
        message_ids: list[int],
        to_chat: str | int,
        silent: bool = False,
        account: str | None = None,
    ) -> str:
        """Forward messages from one chat to another, keeping the original author label."""
        client = await manager.get(account)
        source = await resolve_chat(client, from_chat, policy)
        target = await resolve_chat(client, to_chat, policy)
        sent = await client.forward_messages(target, message_ids, source, silent=silent)
        new_ids = ", ".join(f"#{m.id}" for m in sent if m is not None)
        return f"Forwarded to chat {await client.get_peer_id(target)} as {new_ids or 'nothing'}."

    @telegram_tool(server, WRITE)
    async def mark_read(chat: str | int, account: str | None = None) -> str:
        """Mark every message in a chat as read, including mentions.

        The other side sees their messages as read.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        await client.send_read_acknowledge(peer, clear_mentions=True)
        return f"Marked chat {await client.get_peer_id(peer)} as read."

    @telegram_tool(server, WRITE)
    async def send_reaction(
        chat: str | int, message_id: int, emoji: str | None = None, account: str | None = None
    ) -> str:
        """Put an emoji reaction on a message, or remove the user's reaction.

        Args:
            emoji: a single standard emoji such as "👍"; omit to remove the reaction.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        reaction = [types.ReactionEmoji(emoticon=emoji)] if emoji else []
        await client(
            functions.messages.SendReactionRequest(peer=peer, msg_id=message_id, reaction=reaction)
        )
        return (
            f"Reacted {emoji} to #{message_id}." if emoji else f"Removed reaction on #{message_id}."
        )

    @telegram_tool(server, WRITE)
    async def pin_message(
        chat: str | int,
        message_id: int,
        unpin: bool = False,
        notify: bool = False,
        account: str | None = None,
    ) -> str:
        """Pin or unpin a message in a chat.

        Args:
            notify: send members a notification about the pin.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        if unpin:
            await client.unpin_message(peer, message_id)
            return f"Unpinned message #{message_id}."
        await client.pin_message(peer, message_id, notify=notify)
        return f"Pinned message #{message_id}."

    @telegram_tool(server, WRITE)
    async def save_draft(chat: str | int, text: str, account: str | None = None) -> str:
        """Put text into the chat's input field as a draft, without sending it.

        Use this when the user wants to review and send a reply themselves. Empty text
        clears the draft.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        await client(functions.messages.SaveDraftRequest(peer=peer, message=text))
        return f"Draft saved in chat {await client.get_peer_id(peer)}; nothing was sent."

    @telegram_tool(server, WRITE)
    async def archive_chat(
        chat: str | int, archived: bool = True, account: str | None = None
    ) -> str:
        """Move a chat to Telegram's Archive folder, or back with `archived=false`.

        This is the folder in the Telegram app. It is unrelated to the history_* tools.
        """
        client = await manager.get(account)
        peer = await resolve_chat(client, chat, policy)
        await client.edit_folder(peer, 1 if archived else 0)
        return f"Chat {await client.get_peer_id(peer)} {'archived' if archived else 'unarchived'}."

    @telegram_tool(server, WRITE)
    async def join_chat(link: str, account: str | None = None) -> str:
        """Join a public group or channel by @username or link, or a private one by invite link."""
        client = await manager.get(account)
        invite_hash, is_invite = utils.parse_username(link.strip())
        if is_invite:
            updates = await client(functions.messages.ImportChatInviteRequest(invite_hash))
        else:
            address = link if "/" in link or link.startswith("@") else f"@{link}"
            peer = await resolve_chat(client, address, policy)
            updates = await client(functions.channels.JoinChannelRequest(peer))
        joined = ", ".join(format_entity(chat) for chat in updates.chats)
        return f"Joined: {joined}" if joined else "Join request sent."

    @telegram_tool(server, DESTRUCTIVE)
    async def leave_chat(chat: str | int, account: str | None = None) -> str:
        """Leave a group or channel. Private groups cannot be rejoined without a new invite."""
        client = await manager.get(account)
        entity = await client.get_entity(await resolve_chat(client, chat, policy))
        if isinstance(entity, types.Channel):
            await client(functions.channels.LeaveChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            await client(functions.messages.DeleteChatUserRequest(entity.id, types.InputUserSelf()))
        else:
            raise TelegramMCPError("leave_chat only works for groups and channels, not users.")
        return f"Left: {format_entity(entity)}"
