from datetime import UTC, datetime, timedelta

import pytest
from telethon import types
from telethon.tl.custom import Message

from telegram_mcp.formatting import (
    entity_kind,
    format_entity,
    format_message,
    format_size,
    parse_datetime,
)

WHEN = datetime(2026, 3, 5, 12, 30, tzinfo=UTC)


def _message(**kwargs: object) -> Message:
    defaults: dict[str, object] = {"id": 10, "peer_id": types.PeerUser(777), "date": WHEN}
    return Message(**{**defaults, **kwargs})


def test_plain_incoming_message() -> None:
    text = format_message(_message(message="hello", from_id=types.PeerUser(777)))
    local = WHEN.astimezone().strftime("%Y-%m-%d %H:%M")
    assert text == f"#10 | {local} | [777]\nhello"


def test_outgoing_reply_edited_with_reactions() -> None:
    message = _message(
        message="ok",
        out=True,
        reply_to=types.MessageReplyHeader(reply_to_msg_id=9),
        edit_date=WHEN + timedelta(minutes=1),
        reactions=types.MessageReactions(
            results=[types.ReactionCount(reaction=types.ReactionEmoji(emoticon="👍"), count=2)]
        ),
    )
    header, body = format_message(message).split("\n")
    assert header.split(" | ")[2:] == ["me", "reply to #9", "edited", "reactions 👍x2"]
    assert body == "ok"


def test_document_is_described_in_header() -> None:
    document = types.Document(
        id=1,
        access_hash=1,
        file_reference=b"",
        date=WHEN,
        mime_type="application/pdf",
        size=2 * 1024 * 1024,
        dc_id=2,
        attributes=[types.DocumentAttributeFilename("contract.pdf")],
    )
    message = _message(message="", media=types.MessageMediaDocument(document=document))
    assert format_message(message).endswith("[file contract.pdf, 2.0 MB]")


def test_service_message() -> None:
    message = _message(action=types.MessageActionChatJoinedByLink(inviter_id=5))
    assert format_message(message).endswith("[service: ChatJoinedByLink]")


def test_forwarded_from_hidden_sender() -> None:
    message = _message(message="fwd", fwd_from=types.MessageFwdHeader(date=WHEN, from_name="Anna"))
    assert "fwd from Anna" in format_message(message)


def test_entity_line_uses_marked_ids() -> None:
    channel = types.Channel(
        id=1234567890, title="News", photo=types.ChatPhotoEmpty(), date=WHEN, username="news"
    )
    assert format_entity(channel) == "-1001234567890 | channel | News | @news"
    assert entity_kind(types.User(id=1, bot=True)) == "bot"
    assert entity_kind(types.Chat(1, "g", types.ChatPhotoEmpty(), 3, WHEN, 1)) == "group"


def test_parse_datetime_treats_naive_values_as_local() -> None:
    parsed = parse_datetime("2026-01-31T18:30")
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == datetime(2026, 1, 31, 18, 30)
    assert parse_datetime("2026-01-31T18:30+00:00") == datetime(2026, 1, 31, 18, 30, tzinfo=UTC)


def test_parse_datetime_error_shows_expected_format() -> None:
    with pytest.raises(ValueError, match="ISO format"):
        parse_datetime("yesterday")


@pytest.mark.parametrize(
    ("size", "expected"),
    [(None, "? B"), (512, "512 B"), (1536, "1.5 KB"), (5 * 1024**3, "5.0 GB")],
)
def test_format_size(size: int | None, expected: str) -> None:
    assert format_size(size) == expected
