from datetime import timedelta
from types import SimpleNamespace

import pytest
from fakes import (
    AZIZ,
    ME,
    SHOP_BOT,
    TELEGRAM_SERVICE,
    WHEN,
    WORK_GROUP,
    FakeCore,
    FakeTelegram,
)
from telethon import types
from telethon.errors import FloodWaitError
from telethon.tl.custom import Message

from telegram_mcp import sync
from telegram_mcp.archive import ArchiveError, Cursor
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.sync import Fetch, message_record, plan_fetches, should_archive, sync_account

POLICY = ChatPolicy()


def _dialog(chat_id: int, entity: object) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, entity=entity)


def test_a_chat_never_archived_gets_its_newest_messages() -> None:
    assert plan_fetches(None, top_id=900, initial_limit=300, backfill=0) == [Fetch(limit=300)]
    assert plan_fetches(None, top_id=900, initial_limit=300, backfill=50) == [Fetch(limit=350)]
    assert plan_fetches(None, top_id=0, initial_limit=300, backfill=0) == []  # empty chat


def test_an_up_to_date_chat_costs_no_requests() -> None:
    cursor = Cursor(oldest_id=600, newest_id=900, count=300)
    assert plan_fetches(cursor, top_id=900, initial_limit=300, backfill=0) == []


def test_new_messages_are_read_oldest_first_from_the_archived_end() -> None:
    cursor = Cursor(oldest_id=600, newest_id=900, count=300)
    (fetch,) = plan_fetches(cursor, top_id=950, initial_limit=300, backfill=0)
    assert (fetch.min_id, fetch.reverse) == (900, True)


def test_backfill_reads_below_the_archived_range_and_stops_at_the_first_message() -> None:
    cursor = Cursor(oldest_id=600, newest_id=900, count=300)
    assert plan_fetches(cursor, top_id=900, initial_limit=300, backfill=200) == [
        Fetch(max_id=600, limit=200)
    ]
    reached_start = Cursor(oldest_id=1, newest_id=900, count=900)
    assert plan_fetches(reached_start, top_id=900, initial_limit=300, backfill=200) == []


def test_only_personal_chats_are_archived_without_opt_in() -> None:
    nobody: frozenset[int] = frozenset()
    user = _dialog(AZIZ, types.User(id=AZIZ, first_name="Aziz"))
    bot = _dialog(SHOP_BOT, types.User(id=SHOP_BOT, first_name="Shop", bot=True))
    group = _dialog(WORK_GROUP, types.Channel(123, "Work", types.ChatPhotoEmpty(), WHEN))

    assert should_archive(user, nobody, POLICY)
    assert not should_archive(bot, nobody, POLICY)
    assert not should_archive(group, nobody, POLICY)
    assert should_archive(group, frozenset({WORK_GROUP}), POLICY)
    assert should_archive(bot, frozenset({SHOP_BOT}), POLICY)


def test_hidden_chats_are_never_archived_even_if_opted_in() -> None:
    service = _dialog(TELEGRAM_SERVICE, types.User(id=TELEGRAM_SERVICE, first_name="Telegram"))
    assert not should_archive(service, frozenset({TELEGRAM_SERVICE}), POLICY)
    user = _dialog(AZIZ, types.User(id=AZIZ, first_name="Aziz"))
    assert not should_archive(user, frozenset(), ChatPolicy([AZIZ]))


def test_message_record_matches_the_wire_format() -> None:
    message = Message(
        id=42,
        peer_id=types.PeerUser(AZIZ),
        date=WHEN,
        message="**not** markdown-rendered",
        from_id=types.PeerUser(AZIZ),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=40),
        edit_date=WHEN + timedelta(minutes=3),
        fwd_from=types.MessageFwdHeader(date=WHEN, from_name="Anna"),
    )
    assert message_record(message, AZIZ) == {
        "chat_id": AZIZ,
        "message_id": 42,
        "sent_at": "2026-11-01T12:00:00+00:00",
        "sender_id": AZIZ,
        "sender_name": "",
        "text": "**not** markdown-rendered",
        "reply_to_id": 40,
        "media": None,
        "forwarded_from": "fwd from Anna",
        "edited_at": "2026-11-01T12:03:00+00:00",
        "is_outgoing": False,
    }


def test_service_events_are_recorded_as_a_media_label() -> None:
    message = Message(
        id=7,
        peer_id=types.PeerChat(1),
        date=WHEN,
        action=types.MessageActionChatJoinedByLink(inviter_id=5),
    )
    record = message_record(message, -1)
    assert (record["text"], record["media"]) == ("", "service: ChatJoinedByLink")


async def test_first_run_uploads_personal_chats_only() -> None:
    telegram = FakeTelegram(
        {
            AZIZ: list(range(1, 11)),
            WORK_GROUP: [1, 2, 3],
            SHOP_BOT: [1, 2],
            TELEGRAM_SERVICE: [1, 2, 3],
        }
    )
    core = FakeCore()
    report = await sync_account(telegram, core.archive(), POLICY, initial_limit=4)

    assert core.ids(AZIZ) == [7, 8, 9, 10]  # the newest `initial_limit`
    assert {chat_id for chat_id, _ in core.messages} == {AZIZ}
    assert [chat_id for chat_id, _ in telegram.fetches] == [AZIZ]  # others were not even read
    assert (report.chats_checked, report.chats_updated, report.new_messages) == (1, 1, 4)
    assert report.complete
    assert core.chats[AZIZ] == {"chat_id": AZIZ, "kind": "user", "title": "Aziz", "username": None}


async def test_later_runs_upload_only_what_is_new() -> None:
    telegram = FakeTelegram({AZIZ: list(range(1, 11))})
    core = FakeCore()
    await sync_account(telegram, core.archive(), POLICY, initial_limit=4)

    telegram.fetches.clear()
    report = await sync_account(telegram, core.archive(), POLICY, initial_limit=4)
    assert (telegram.fetches, report.new_messages) == ([], 0)

    telegram.history[AZIZ] += [11, 12]
    report = await sync_account(telegram, core.archive(), POLICY, initial_limit=4)
    assert core.ids(AZIZ) == [7, 8, 9, 10, 11, 12]
    assert report.new_messages == 2


async def test_opted_in_group_is_archived_and_backfill_extends_downwards() -> None:
    telegram = FakeTelegram({AZIZ: list(range(1, 11)), WORK_GROUP: [1, 2, 3]})
    core = FakeCore()
    core.selected.append(WORK_GROUP)
    await sync_account(telegram, core.archive(), POLICY, initial_limit=4)
    assert core.ids(WORK_GROUP) == [1, 2, 3]
    assert core.chats[WORK_GROUP]["kind"] == "supergroup"

    await sync_account(telegram, core.archive(), POLICY, initial_limit=4, backfill=3)
    assert core.ids(AZIZ) == [4, 5, 6, 7, 8, 9, 10]  # one contiguous range, no hole


async def test_only_chat_limits_the_run_to_one_chat() -> None:
    telegram = FakeTelegram({AZIZ: [1, 2], WORK_GROUP: [1, 2]})
    core = FakeCore()
    core.selected.append(WORK_GROUP)
    await sync_account(telegram, core.archive(), POLICY, initial_limit=10, only_chat_id=WORK_GROUP)
    assert {chat_id for chat_id, _ in core.messages} == {WORK_GROUP}


async def test_large_chats_are_pushed_in_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "BATCH_SIZE", 3)
    telegram = FakeTelegram({AZIZ: list(range(1, 9))})
    core = FakeCore()
    await sync_account(telegram, core.archive(), POLICY, initial_limit=8)
    assert core.batches == [3, 3, 2]
    assert core.ids(AZIZ) == list(range(1, 9))


async def test_an_exhausted_time_budget_stops_before_reading_telegram() -> None:
    telegram = FakeTelegram({AZIZ: [1, 2, 3]})
    core = FakeCore()
    report = await sync_account(telegram, core.archive(), POLICY, initial_limit=4, max_seconds=0)
    assert not report.complete
    assert telegram.fetches == []
    assert "run the sync again" in report.summary()


async def test_a_long_flood_wait_ends_the_run_instead_of_hanging() -> None:
    telegram = FakeTelegram({AZIZ: [1, 2, 3]})
    telegram.fail_with = FloodWaitError(request=None, capture=3600)
    report = await sync_account(
        telegram, FakeCore().archive(), POLICY, initial_limit=4, max_flood_wait=60
    )
    assert not report.complete
    assert "wait 3600s" in report.summary()


async def test_a_short_flood_wait_is_slept_through(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("telegram_mcp.sync.asyncio.sleep", fake_sleep)
    telegram = FakeTelegram({AZIZ: [1, 2, 3]})
    telegram.fail_with = FloodWaitError(request=None, capture=5)
    core = FakeCore()
    report = await sync_account(
        telegram, core.archive(), POLICY, initial_limit=4, max_flood_wait=60
    )
    assert core.ids(AZIZ) == [1, 2, 3]
    assert slept == [6]
    assert report.complete and report.new_messages == 3  # retried in the same run


async def test_a_wrong_token_is_reported_in_plain_words() -> None:
    telegram = FakeTelegram({AZIZ: [1]})
    with pytest.raises(ArchiveError, match="TELEGRAM_MCP_CORE_TOKEN"):
        await sync_account(telegram, FakeCore().archive(token="wrong"), POLICY, initial_limit=4)
    assert telegram.fetches == []  # nothing is read from Telegram if it cannot be stored


def test_the_account_is_identified_by_its_own_user_id() -> None:
    assert ME != AZIZ  # guards the fixture the tests above rely on


async def test_a_flood_wait_longer_than_the_time_budget_ends_the_run() -> None:
    telegram = FakeTelegram({AZIZ: [1, 2, 3]})
    telegram.fail_with = FloodWaitError(request=None, capture=30)
    report = await sync_account(
        telegram, FakeCore().archive(), POLICY, initial_limit=4, max_seconds=5, max_flood_wait=60
    )
    assert not report.complete
    assert "wait 30s" in report.summary()
