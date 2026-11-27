"""History archive kept on this computer: SQLite with a full-text index.

For people without their own server. It speaks the same interface as
CoreArchive (sync_state, ingest, set_selection, search, chats), so the sync and
the history_* tools do not care where the archive lives. The schema and the
index rules mirror core's modules/tg_archive/repository.py.

sqlite3 is synchronous, so every call runs in a worker thread with its own
connection; WAL lets a search run while another process is writing a batch.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from telegram_mcp.archive import ArchiveError, Cursor, SyncState
from telegram_mcp.search_query import build_match_query, make_snippet, normalize

T = TypeVar("T")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    account_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    username TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    sender_id INTEGER,
    sender_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    reply_to_id INTEGER,
    media TEXT,
    forwarded_from TEXT,
    edited_at TEXT,
    is_outgoing INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT NOT NULL,
    UNIQUE (account_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS messages_by_date ON messages (account_id, chat_id, sent_at);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(text, media, content='', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS selected_chats (
    account_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (account_id, chat_id)
);
"""

_FTS_INSERT = "INSERT INTO messages_fts (rowid, text, media) VALUES (?, ?, ?)"
_FTS_DELETE = (
    "INSERT INTO messages_fts (messages_fts, rowid, text, media) VALUES ('delete', ?, ?, ?)"
)
_BUSY_TIMEOUT_S = 30.0
_MAX_LIMIT = 100


def _iso(value: str | datetime | None) -> str | None:
    """UTC ISO string with second precision; accepts what the wire format carries."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fts_row(rowid: int | None, text: str, media: str | None) -> tuple[int | None, str, str]:
    return rowid, normalize(text), normalize(media or "")


class LocalArchive:
    """The archive in one SQLite file. Safe to use from several processes at once."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_S)
        try:
            yield db
        finally:
            db.close()

    def _init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            db.commit()
        self._ready = True

    async def _run(self, fn: Callable[[], T]) -> T:
        def call() -> T:
            if not self._ready:
                self._init()
            return fn()

        try:
            return await asyncio.to_thread(call)
        except sqlite3.OperationalError as exc:
            raise ArchiveError(f"Local archive error ({exc}) in {self._db_path}") from exc

    async def aclose(self) -> None:
        """Nothing to close: every call opens and closes its own connection."""

    # -- what the sync needs -------------------------------------------------

    async def sync_state(self, account_id: int) -> SyncState:
        def read() -> SyncState:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT chat_id, MIN(message_id), MAX(message_id), COUNT(*) FROM messages "
                    "WHERE account_id = ? GROUP BY chat_id",
                    (account_id,),
                ).fetchall()
                selected = db.execute(
                    "SELECT chat_id FROM selected_chats WHERE account_id = ?", (account_id,)
                ).fetchall()
            return SyncState(
                cursors={row[0]: Cursor(row[1], row[2], row[3]) for row in rows},
                selected=frozenset(row[0] for row in selected),
            )

        return await self._run(read)

    async def ingest(
        self, account_id: int, chats: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Upsert in one transaction; an unchanged message is a no-op, so batches can repeat."""

        def write() -> tuple[int, int]:
            stamp = _now()
            new = updated = 0
            with self._connect() as db:
                for chat in chats:
                    db.execute(
                        "INSERT INTO chats (account_id, chat_id, kind, title, username, "
                        "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (account_id, chat_id) DO UPDATE SET kind = excluded.kind, "
                        "title = excluded.title, username = excluded.username, "
                        "updated_at = excluded.updated_at",
                        (
                            account_id,
                            chat["chat_id"],
                            chat["kind"],
                            chat["title"],
                            chat.get("username"),
                            stamp,
                        ),
                    )
                for message in messages:
                    text = message.get("text") or ""
                    media = message.get("media")
                    edited_at = _iso(message.get("edited_at"))
                    row = db.execute(
                        "SELECT id, text, media, edited_at FROM messages "
                        "WHERE account_id = ? AND chat_id = ? AND message_id = ?",
                        (account_id, message["chat_id"], message["message_id"]),
                    ).fetchone()
                    if row is None:
                        cursor = db.execute(
                            "INSERT INTO messages (account_id, chat_id, message_id, sent_at, "
                            "sender_id, sender_name, text, reply_to_id, media, forwarded_from, "
                            "edited_at, is_outgoing, archived_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                account_id,
                                message["chat_id"],
                                message["message_id"],
                                _iso(message["sent_at"]),
                                message.get("sender_id"),
                                message.get("sender_name") or "",
                                text,
                                message.get("reply_to_id"),
                                media,
                                message.get("forwarded_from"),
                                edited_at,
                                bool(message.get("is_outgoing")),
                                stamp,
                            ),
                        )
                        db.execute(_FTS_INSERT, _fts_row(cursor.lastrowid, text, media))
                        new += 1
                    elif (row[1], row[2], row[3]) != (text, media, edited_at):
                        db.execute(_FTS_DELETE, _fts_row(row[0], row[1], row[2]))
                        db.execute(
                            "UPDATE messages SET text = ?, media = ?, edited_at = ?, "
                            "sender_name = ?, forwarded_from = ?, reply_to_id = ?, "
                            "archived_at = ? WHERE id = ?",
                            (
                                text,
                                media,
                                edited_at,
                                message.get("sender_name") or "",
                                message.get("forwarded_from"),
                                message.get("reply_to_id"),
                                stamp,
                                row[0],
                            ),
                        )
                        db.execute(_FTS_INSERT, _fts_row(row[0], text, media))
                        updated += 1
                db.commit()
            return new, updated

        return await self._run(write)

    async def set_selection(
        self,
        account_id: int,
        chat_id: int,
        *,
        selected: bool,
        title: str = "",
        kind: str = "",
        purge: bool = False,
    ) -> int:
        """Opt a chat in or out; returns how many stored messages were purged."""

        def write() -> int:
            purged = 0
            with self._connect() as db:
                if selected:
                    db.execute(
                        "INSERT INTO selected_chats (account_id, chat_id, title, kind, added_at) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (account_id, chat_id) "
                        "DO UPDATE SET title = excluded.title, kind = excluded.kind",
                        (account_id, chat_id, title, kind, _now()),
                    )
                else:
                    db.execute(
                        "DELETE FROM selected_chats WHERE account_id = ? AND chat_id = ?",
                        (account_id, chat_id),
                    )
                    if purge:
                        rows = db.execute(
                            "SELECT id, text, media FROM messages "
                            "WHERE account_id = ? AND chat_id = ?",
                            (account_id, chat_id),
                        ).fetchall()
                        db.executemany(_FTS_DELETE, [_fts_row(*row) for row in rows])
                        db.execute(
                            "DELETE FROM messages WHERE account_id = ? AND chat_id = ?",
                            (account_id, chat_id),
                        )
                        db.execute(
                            "DELETE FROM chats WHERE account_id = ? AND chat_id = ?",
                            (account_id, chat_id),
                        )
                        purged = len(rows)
                db.commit()
            return purged

        return await self._run(write)

    # -- what the history_* tools need ----------------------------------------

    async def search(
        self,
        *,
        q: str = "",
        account_id: int | None = None,
        chat_id: int | None = None,
        sender_id: int | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        sort: str = "relevance",
    ) -> list[dict[str, Any]]:
        """Find messages by text and/or filters; an empty q lists them newest first."""
        match = build_match_query(q)
        conditions: list[str] = []
        params: list[object] = []
        if match is not None:
            conditions.append("messages_fts MATCH ?")
            params.append(match)
        for clause, value in (
            ("m.account_id = ?", account_id),
            ("m.chat_id = ?", chat_id),
            ("m.sender_id = ?", sender_id),
            ("m.sent_at >= ?", _iso(since)),
            ("m.sent_at < ?", _iso(until)),
        ):
            if value is not None:
                conditions.append(clause)
                params.append(value)
        source = "messages m"
        order = "m.sent_at DESC"
        if match is not None:
            source = "messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            if sort == "relevance":
                order = "messages_fts.rank, m.sent_at DESC"
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        # Only fixed fragments are interpolated; every value travels as a parameter.
        sql = (
            "SELECT m.account_id, m.chat_id, m.message_id, m.sent_at, m.sender_id, "
            "m.sender_name, m.text, m.reply_to_id, m.media, m.is_outgoing, "
            "COALESCE(c.title, ''), COALESCE(c.kind, '') "
            f"FROM {source} "
            "LEFT JOIN chats c ON c.account_id = m.account_id AND c.chat_id = m.chat_id "
            f"{where} ORDER BY {order} LIMIT ?"
        )
        bounded = max(1, min(limit, _MAX_LIMIT))

        def read() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(sql, (*params, bounded)).fetchall()
            return [
                {
                    "account_id": row[0],
                    "chat_id": row[1],
                    "message_id": row[2],
                    "sent_at": row[3],
                    "sender_id": row[4],
                    "sender_name": row[5],
                    "text": make_snippet(row[6], q),
                    "reply_to_id": row[7],
                    "media": row[8],
                    "is_outgoing": bool(row[9]),
                    "chat_title": row[10],
                    "chat_kind": row[11],
                }
                for row in rows
            ]

        return await self._run(read)

    async def chats(self, account_id: int) -> list[dict[str, Any]]:
        """Every archived or opted-in chat of the account, most recently active first."""

        def read() -> list[dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT ids.chat_id, COALESCE(c.title, s.title, ''), "
                    "COALESCE(c.kind, s.kind, ''), COUNT(m.id), MIN(m.sent_at), "
                    "MAX(m.sent_at), s.chat_id IS NOT NULL "
                    "FROM (SELECT chat_id FROM chats WHERE account_id = :acc "
                    "      UNION SELECT chat_id FROM selected_chats WHERE account_id = :acc) ids "
                    "LEFT JOIN chats c ON c.account_id = :acc AND c.chat_id = ids.chat_id "
                    "LEFT JOIN selected_chats s "
                    "  ON s.account_id = :acc AND s.chat_id = ids.chat_id "
                    "LEFT JOIN messages m ON m.account_id = :acc AND m.chat_id = ids.chat_id "
                    "GROUP BY ids.chat_id ORDER BY MAX(m.sent_at) DESC",
                    {"acc": account_id},
                ).fetchall()
            return [
                {
                    "chat_id": row[0],
                    "title": row[1],
                    "kind": row[2],
                    "messages": row[3],
                    "oldest_at": row[4],
                    "newest_at": row[5],
                    "selected": bool(row[6]),
                }
                for row in rows
            ]

        return await self._run(read)
