"""Render Telegram objects as compact text.

Tools return plain text rather than JSON: reading a few hundred messages is the
main use case, and one line per message costs far fewer tokens.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telethon import types, utils

_PREVIEW_LENGTH = 80


def local_timezone_label() -> str:
    offset = datetime.now().astimezone().strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "?"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def parse_datetime(value: str) -> datetime:
    """Parse an ISO date or datetime. Values without an offset are local time."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(
            f"Invalid date {value!r}: use ISO format like 2026-01-31 or 2026-01-31T18:30"
        ) from exc
    return parsed.astimezone()


def format_size(size: int | None) -> str:
    if size is None:
        return "? B"
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def entity_kind(entity: Any) -> str:
    if isinstance(entity, types.User):
        return "bot" if entity.bot else "user"
    if isinstance(entity, types.Channel):
        if entity.megagroup:
            return "forum" if getattr(entity, "forum", False) else "supergroup"
        return "channel"
    if isinstance(entity, types.Chat | types.ChatForbidden):
        return "group"
    if isinstance(entity, types.ChannelForbidden):
        return "channel"
    return "unknown"


def display_name(entity: Any) -> str:
    return utils.get_display_name(entity) or "(no name)"


def format_entity(entity: Any) -> str:
    """One line: `id | kind | name | @username`."""
    parts = [str(utils.get_peer_id(entity)), entity_kind(entity), display_name(entity)]
    username = getattr(entity, "username", None)
    if username:
        parts.append(f"@{username}")
    return " | ".join(parts)


def is_muted(dialog: Any) -> bool:
    settings = getattr(dialog.dialog, "notify_settings", None)
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None:
        return False
    return bool(mute_until.astimezone() > datetime.now().astimezone())


def format_dialog(dialog: Any) -> str:
    parts = [format_entity(dialog.entity)]
    if dialog.unread_count:
        unread = f"unread {dialog.unread_count}"
        if dialog.unread_mentions_count:
            unread += f" ({dialog.unread_mentions_count} mentions)"
        parts.append(unread)
    flags = [
        name
        for name, enabled in (
            ("pinned", dialog.pinned),
            ("archived", dialog.archived),
            ("muted", is_muted(dialog)),
        )
        if enabled
    ]
    if flags:
        parts.append(",".join(flags))
    if dialog.message is not None:
        parts.append(f"last {format_datetime(dialog.message.date)}: {_preview(dialog.message)}")
    return " | ".join(parts)


def _preview(message: Any) -> str:
    text = " ".join(message_text(message).split())
    if not text:
        text = f"[{describe_media(message) or describe_action(message) or 'empty'}]"
    return text if len(text) <= _PREVIEW_LENGTH else text[: _PREVIEW_LENGTH - 1] + "…"


def message_text(message: Any) -> str:
    # `.text` renders hidden links and formatting as markdown, but needs a bound client.
    return message.text or message.message or ""


def describe_action(message: Any) -> str | None:
    action = getattr(message, "action", None)
    if action is None:
        return None
    return type(action).__name__.removeprefix("MessageAction")


def describe_media(message: Any) -> str | None:
    """Short label for a message attachment, or None if there is nothing worth naming."""
    if message.media is None:
        return None
    file = message.file
    size = format_size(file.size) if file is not None else ""
    if message.photo:
        return "photo"
    if message.sticker:
        return f"sticker {file.emoji or ''}".strip()
    if message.voice:
        return f"voice {file.duration or 0}s"
    if message.video_note:
        return f"video note {file.duration or 0}s"
    if message.gif:
        return "gif"
    if message.video:
        return f"video {file.duration or 0}s, {size}"
    if message.audio:
        title = " - ".join(part for part in (file.performer, file.title) if part) or file.name
        return f"audio {title or ''}".strip() + f", {size}"
    if message.document:
        return f"file {file.name or file.mime_type}, {size}"
    if message.poll:
        poll = message.poll.poll
        options = ", ".join(_plain(answer.text) for answer in poll.answers)
        return f"poll: {_plain(poll.question)} ({options})"
    if message.geo:
        return f"location {message.geo.lat:.5f},{message.geo.long:.5f}"
    if message.contact:
        contact = message.contact
        name = f"{contact.first_name} {contact.last_name}".strip()
        return f"contact {name} +{contact.phone_number.lstrip('+')}"
    if isinstance(message.media, types.MessageMediaWebPage):
        return None  # the link itself is already in the text
    return type(message.media).__name__.removeprefix("MessageMedia").lower()


def _plain(value: Any) -> str:
    # Poll texts are plain strings in older API layers and TextWithEntities in newer ones.
    return str(getattr(value, "text", value))


def _sender_label(message: Any) -> str:
    if message.out:
        return "me"
    sender = message.sender
    if sender is not None:
        return f"{display_name(sender)} [{message.sender_id}]"
    if message.sender_id:
        return f"[{message.sender_id}]"
    return "channel post"


def forward_label(message: Any) -> str | None:
    fwd = message.fwd_from
    if fwd is None:
        return None
    forward = message.forward
    origin = getattr(forward, "sender", None) or getattr(forward, "chat", None)
    name = display_name(origin) if origin is not None else fwd.from_name
    return f"fwd from {name}" if name else "forwarded"


def _reactions_label(message: Any) -> str | None:
    reactions = getattr(message, "reactions", None)
    results = getattr(reactions, "results", None)
    if not results:
        return None
    return " ".join(
        f"{getattr(item.reaction, 'emoticon', None) or 'custom'}x{item.count}" for item in results
    )


def format_message(message: Any, *, with_chat: bool = False) -> str:
    """Header line `#id | time | sender | ...flags`, then the text on following lines."""
    header = [f"#{message.id}", format_datetime(message.date)]
    if with_chat and message.chat is not None:
        header.append(f"in {display_name(message.chat)} [{message.chat_id}]")
    header.append(_sender_label(message))

    reply_to = getattr(message.reply_to, "reply_to_msg_id", None)
    if reply_to:
        header.append(f"reply to #{reply_to}")
    forward = forward_label(message)
    if forward:
        header.append(forward)
    if message.edit_date:
        header.append("edited")
    if getattr(message, "pinned", False):
        header.append("pinned")
    attachment = describe_media(message)
    if attachment:
        header.append(f"[{attachment}]")
    action = describe_action(message)
    if action:
        header.append(f"[service: {action}]")
    reactions = _reactions_label(message)
    if reactions:
        header.append(f"reactions {reactions}")

    text = message_text(message)
    line = " | ".join(header)
    return f"{line}\n{text}" if text else line


def format_messages(messages: list[Any], *, with_chat: bool = False) -> str:
    return "\n\n".join(format_message(m, with_chat=with_chat) for m in messages)
