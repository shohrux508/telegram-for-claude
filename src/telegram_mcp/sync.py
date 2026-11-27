"""Pushes Telegram history from this machine to the archive on the core server.

The server is the source of truth for progress: before each run it reports the
id range it already holds per chat, so a run interrupted anywhere (flood wait,
sleep, crash) is resumed by simply running again.

Invariant: what the server holds for a chat is always one contiguous id range.
Every fetch extends that range from one of its ends, and batches are pushed in
fetch order, so stopping mid-fetch never leaves a hole.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from telethon.errors import FloodWaitError

from telegram_mcp.archive import Archive, Cursor
from telegram_mcp.formatting import (
    describe_action,
    describe_media,
    display_name,
    entity_kind,
    forward_label,
)
from telegram_mcp.policy import ChatPolicy

log = structlog.get_logger(__name__)

BATCH_SIZE = 500
# Upper bound of new messages taken from one chat per run; the rest waits for the next run.
MAX_NEW_PER_RUN = 5000
# Personal chats are always archived; anything else only if the owner opted it in.
ALWAYS_ARCHIVED_KIND = "user"


@dataclass(frozen=True)
class Fetch:
    """One pass over a chat's history; fields mirror Telethon's iter_messages arguments."""

    min_id: int = 0
    max_id: int = 0
    limit: int | None = None
    reverse: bool = False


@dataclass
class SyncReport:
    chats_checked: int = 0
    chats_updated: int = 0
    new_messages: int = 0
    updated_messages: int = 0
    complete: bool = True
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"Checked {self.chats_checked} chats, updated {self.chats_updated}: "
            f"{self.new_messages} new messages, {self.updated_messages} changed."
        )
        if not self.complete:
            text += " Not finished: run the sync again to continue."
        return " ".join([text, *self.notes])


def plan_fetches(
    cursor: Cursor | None, top_id: int, *, initial_limit: int, backfill: int
) -> list[Fetch]:
    """Decide what to read from a chat given what the server already holds.

    cursor: the archived id range, None for a chat never archived.
    top_id: id of the chat's newest message right now.
    backfill: how many messages older than the archived range to add (0 = none).
    """
    if cursor is None:
        return [Fetch(limit=initial_limit + backfill)] if top_id else []
    fetches: list[Fetch] = []
    if top_id > cursor.newest_id:
        # Oldest first, so a partial run still extends the range without a gap.
        fetches.append(Fetch(min_id=cursor.newest_id, reverse=True, limit=MAX_NEW_PER_RUN))
    if backfill and cursor.oldest_id > 1:
        fetches.append(Fetch(max_id=cursor.oldest_id, limit=backfill))
    return fetches


def should_archive(dialog: Any, selected: frozenset[int], policy: ChatPolicy) -> bool:
    if policy.is_hidden(dialog.id):
        return False
    return entity_kind(dialog.entity) == ALWAYS_ARCHIVED_KIND or dialog.id in selected


def chat_record(dialog: Any) -> dict[str, Any]:
    return {
        "chat_id": dialog.id,
        "kind": entity_kind(dialog.entity),
        "title": dialog.name or "",
        "username": getattr(dialog.entity, "username", None),
    }


def message_record(message: Any, chat_id: int) -> dict[str, Any]:
    """Serialize a Telethon message into the archive's wire format."""
    action = describe_action(message)
    sender = message.sender
    return {
        "chat_id": chat_id,
        "message_id": message.id,
        "sent_at": message.date.isoformat(),
        "sender_id": message.sender_id,
        "sender_name": display_name(sender) if sender is not None else "",
        # Raw text, not the markdown rendering: formatting marks would pollute search.
        "text": message.message or "",
        "reply_to_id": getattr(message.reply_to, "reply_to_msg_id", None),
        "media": describe_media(message) or (f"service: {action}" if action else None),
        "forwarded_from": forward_label(message),
        "edited_at": message.edit_date.isoformat() if message.edit_date else None,
        "is_outgoing": bool(message.out),
    }


async def sync_account(
    client: Any,
    archive: Archive,
    policy: ChatPolicy,
    *,
    initial_limit: int,
    backfill: int = 0,
    only_chat_id: int | None = None,
    max_seconds: float | None = None,
    max_flood_wait: int = 60,
    progress: Callable[[str], None] | None = None,
) -> SyncReport:
    """Bring the archive up to date for one logged-in account.

    max_seconds: stop starting new work after this long (None = run to the end).
    max_flood_wait: sleep through Telegram rate limits up to this many seconds;
        a longer one ends the run, to be resumed later.
    """
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    report = SyncReport()
    me = await client.get_me()
    state = await archive.sync_state(me.id)

    async for dialog in client.iter_dialogs(ignore_migrated=True):
        if only_chat_id is not None and dialog.id != only_chat_id:
            continue
        if not should_archive(dialog, state.selected, policy):
            continue
        report.chats_checked += 1
        top_id = dialog.message.id if dialog.message is not None else 0
        fetches = plan_fetches(
            state.cursors.get(dialog.id), top_id, initial_limit=initial_limit, backfill=backfill
        )
        if not fetches:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            report.complete = False
            break
        try:
            new, updated, finished = await _sync_chat(
                client, archive, me.id, dialog, fetches, deadline
            )
        except FloodWaitError as exc:
            out_of_time = deadline is not None and time.monotonic() + exc.seconds >= deadline
            if exc.seconds > max_flood_wait or out_of_time:
                report.complete = False
                report.notes.append(f"Telegram asked to wait {exc.seconds}s; stopped early.")
                break
            log.info("sync.flood_wait", seconds=exc.seconds, chat_id=dialog.id)
            await asyncio.sleep(exc.seconds + 1)
            # Repeating the fetches is safe: the server skips what it already holds.
            new, updated, finished = await _sync_chat(
                client, archive, me.id, dialog, fetches, deadline
            )
        report.new_messages += new
        report.updated_messages += updated
        report.chats_updated += 1 if new or updated else 0
        report.complete = report.complete and finished
        if progress and (new or updated):
            progress(f"{dialog.name}: +{new} new, {updated} changed")
    return report


async def _sync_chat(
    client: Any,
    archive: Archive,
    account_id: int,
    dialog: Any,
    fetches: list[Fetch],
    deadline: float | None,
) -> tuple[int, int, bool]:
    """Run the planned fetches for one chat. Returns (new, updated, finished)."""
    chat = [chat_record(dialog)]
    new = updated = 0

    async def push(batch: list[dict[str, Any]]) -> None:
        nonlocal new, updated
        added, changed = await archive.ingest(account_id, chat, batch)
        new += added
        updated += changed

    for fetch in fetches:
        batch: list[dict[str, Any]] = []
        async for message in client.iter_messages(
            dialog.input_entity,
            limit=fetch.limit,
            min_id=fetch.min_id,
            max_id=fetch.max_id,
            reverse=fetch.reverse,
        ):
            batch.append(message_record(message, dialog.id))
            if len(batch) >= BATCH_SIZE:
                await push(batch)
                batch = []
                if deadline is not None and time.monotonic() >= deadline:
                    return new, updated, False
        if batch:
            await push(batch)
    return new, updated, True
