"""The on-disk archive: same behaviour as the one on core, plus sync and tools on top."""

from pathlib import Path
from typing import Any

import pytest
from fakes import AZIZ, WORK_GROUP, FakeTelegram

from telegram_mcp.local_archive import LocalArchive
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.sync import sync_account

ACCOUNT = 1000
CHATS = [
    {"chat_id": AZIZ, "kind": "user", "title": "Aziz", "username": "aziz"},
    {"chat_id": WORK_GROUP, "kind": "supergroup", "title": "Работа", "username": None},
]


def msg(
    message_id: int, text: str, *, chat_id: int = AZIZ, day: int = 1, **extra: Any
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_at": f"2026-11-{day:02d}T12:00:00+05:00",
        "sender_id": AZIZ,
        "sender_name": "Aziz",
        "text": text,
        "reply_to_id": None,
        "media": None,
        "forwarded_from": None,
        "edited_at": None,
        "is_outgoing": False,
        **extra,
    }


@pytest.fixture
def archive(tmp_path: Path) -> LocalArchive:
    return LocalArchive(tmp_path / "sub" / "archive.db")


async def ids(archive: LocalArchive, query: str, **filters: Any) -> list[int]:
    return [hit["message_id"] for hit in await archive.search(q=query, **filters)]


async def test_creates_its_file_on_first_use(archive: LocalArchive, tmp_path: Path) -> None:
    assert not (tmp_path / "sub").exists()
    assert await archive.chats(ACCOUNT) == []
    assert (tmp_path / "sub" / "archive.db").exists()


async def test_ingest_is_idempotent_and_searchable(archive: LocalArchive) -> None:
    batch = [msg(1, "Подписали договор аренды"), msg(2, "ещё раз проверь счёт")]
    assert await archive.ingest(ACCOUNT, CHATS, batch) == (2, 0)
    assert await archive.ingest(ACCOUNT, CHATS, batch) == (0, 0)
    assert await ids(archive, "договора") == [1]
    assert await ids(archive, "еще счет") == [2]
    assert await ids(archive, '"договор аренды"') == [1]


async def test_edits_replace_indexed_text(archive: LocalArchive) -> None:
    await archive.ingest(ACCOUNT, CHATS, [msg(1, "встреча в понедельник")])
    edited = msg(1, "встреча во вторник", edited_at="2026-11-01T12:05:00+05:00")
    assert await archive.ingest(ACCOUNT, CHATS, [edited]) == (0, 1)
    assert await ids(archive, "понедельник") == []
    assert await ids(archive, "вторник") == [1]


async def test_filters_and_hit_shape(archive: LocalArchive) -> None:
    await archive.ingest(
        ACCOUNT,
        CHATS,
        [msg(1, "отчёт", day=1), msg(2, "отчёт", day=5), msg(3, "отчёт", chat_id=WORK_GROUP)],
    )
    assert sorted(await ids(archive, "отчет", chat_id=AZIZ)) == [1, 2]
    assert await ids(archive, "отчет", since="2026-11-03") == [2]
    assert await ids(archive, "", chat_id=AZIZ, limit=1) == [2]
    (hit,) = await archive.search(q="отчет", chat_id=WORK_GROUP)
    assert hit["chat_title"] == "Работа" and hit["chat_kind"] == "supergroup"
    assert hit["sent_at"] == "2026-11-01T07:00:00+00:00"  # stored in UTC


async def test_selection_and_purge(archive: LocalArchive) -> None:
    await archive.ingest(ACCOUNT, CHATS, [msg(1, "секрет", chat_id=WORK_GROUP), msg(2, "секрет")])
    await archive.set_selection(ACCOUNT, WORK_GROUP, selected=True, title="Работа", kind="g")
    assert (await archive.sync_state(ACCOUNT)).selected == {WORK_GROUP}

    purged = await archive.set_selection(ACCOUNT, WORK_GROUP, selected=False, purge=True)
    assert purged == 1
    assert await ids(archive, "секрет") == [2]
    state = await archive.sync_state(ACCOUNT)
    assert (state.selected, set(state.cursors)) == (frozenset(), {AZIZ})


async def test_chats_report_counts_and_selection(archive: LocalArchive) -> None:
    await archive.ingest(ACCOUNT, CHATS[:1], [msg(1, "a"), msg(2, "b")])
    await archive.set_selection(ACCOUNT, WORK_GROUP, selected=True, title="Работа", kind="g")
    chats = {chat["chat_id"]: chat for chat in await archive.chats(ACCOUNT)}
    assert (chats[AZIZ]["messages"], chats[AZIZ]["selected"]) == (2, False)
    assert (chats[WORK_GROUP]["messages"], chats[WORK_GROUP]["selected"]) == (0, True)


async def test_full_sync_into_the_local_archive(archive: LocalArchive) -> None:
    telegram = FakeTelegram({AZIZ: list(range(1, 8))})
    report = await sync_account(telegram, archive, ChatPolicy(), initial_limit=5)
    assert report.new_messages == 5
    state = await archive.sync_state(1000)
    assert (state.cursors[AZIZ].oldest_id, state.cursors[AZIZ].newest_id) == (3, 7)

    telegram.history[AZIZ].append(8)
    report = await sync_account(telegram, archive, ChatPolicy(), initial_limit=5)
    assert report.new_messages == 1
    assert await ids(archive, "message 8") == [8]
