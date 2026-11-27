from types import SimpleNamespace
from typing import Any

import pytest

from telegram_mcp.accounts import (
    AccountManager,
    TelegramMCPError,
    parse_chat_ref,
    resolve_chat,
    validate_account_name,
)
from telegram_mcp.config import Settings
from telegram_mcp.policy import ChatPolicy

POLICY = ChatPolicy()


class FakeClient:
    """Stands in for TelegramClient: knows some peers, lists some dialogs."""

    def __init__(self, known: dict[Any, str], dialogs: list[tuple[int, str]]) -> None:
        self.known = known
        self.dialogs = [
            SimpleNamespace(id=id_, name=name, input_entity=f"peer:{id_}") for id_, name in dialogs
        ]
        self.lookups: list[Any] = []

    async def get_input_entity(self, ref: Any) -> str:
        self.lookups.append(ref)
        if ref in self.known:
            return self.known[ref]
        raise ValueError(f"unknown {ref}")

    async def get_dialogs(self, limit: int | None = None) -> list[SimpleNamespace]:
        # Loading dialogs is what teaches a real session about their ids.
        self.known.update({d.id: d.input_entity for d in self.dialogs})
        return self.dialogs


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (42, 42),
        ("42", 42),
        (" -1001234567890 ", -1001234567890),
        ("@durov", "@durov"),
        ("+79991234567", "+79991234567"),
        ("Family chat", "Family chat"),
    ],
)
def test_parse_chat_ref(raw: str | int, expected: str | int) -> None:
    assert parse_chat_ref(raw) == expected


def test_parse_chat_ref_rejects_empty() -> None:
    with pytest.raises(TelegramMCPError):
        parse_chat_ref("   ")


@pytest.mark.parametrize("name", ["", "has space", "../escape", "a" * 33, "dot.name"])
def test_validate_account_name_rejects_unsafe(name: str) -> None:
    with pytest.raises(TelegramMCPError):
        validate_account_name(name)


def test_account_selection(settings: Settings) -> None:
    manager = AccountManager(settings)
    assert manager.list_accounts() == []
    assert manager.resolve_account_name(None) == "main"

    settings.sessions_dir.mkdir(parents=True)
    (settings.sessions_dir / "work.session").touch()
    assert manager.list_accounts() == ["work"]
    assert manager.resolve_account_name(None) == "work"  # the only one logged in

    (settings.sessions_dir / "personal.session").touch()
    with pytest.raises(TelegramMCPError, match="personal, work"):
        manager.resolve_account_name(None)
    assert manager.resolve_account_name("personal") == "personal"

    (settings.sessions_dir / "main.session").touch()
    assert manager.resolve_account_name(None) == "main"


async def test_get_without_login_explains_how_to_log_in(settings: Settings) -> None:
    with pytest.raises(TelegramMCPError, match="telegram-mcp login --account main"):
        await AccountManager(settings).get()


async def test_resolve_known_id_needs_no_dialog_fetch() -> None:
    client = FakeClient({123: "peer:123"}, [])
    assert await resolve_chat(client, "123", POLICY) == "peer:123"


async def test_resolve_unknown_id_loads_dialogs_then_retries() -> None:
    client = FakeClient({}, [(-100500, "Dev chat")])
    assert await resolve_chat(client, -100500, POLICY) == "peer:-100500"


async def test_resolve_unknown_id_fails_clearly() -> None:
    with pytest.raises(TelegramMCPError, match="No chat with id 7"):
        await resolve_chat(FakeClient({}, []), 7, POLICY)


async def test_resolve_title_is_case_insensitive() -> None:
    client = FakeClient({}, [(1, "Family"), (2, "Work")])
    assert await resolve_chat(client, "family", POLICY) == "peer:1"


async def test_bare_title_is_never_looked_up_as_username() -> None:
    # "@family" exists publicly, but the user means their own chat titled "Family".
    client = FakeClient({"Family": "peer:public-stranger"}, [(1, "Family")])
    assert await resolve_chat(client, "Family", POLICY) == "peer:1"
    assert client.lookups == []


async def test_resolve_ambiguous_title_lists_candidates() -> None:
    client = FakeClient({}, [(1, "Team"), (2, "Team")])
    with pytest.raises(TelegramMCPError, match=r"Team \(id 1\); Team \(id 2\)"):
        await resolve_chat(client, "Team", POLICY)


async def test_resolve_partial_title_suggests_but_does_not_guess() -> None:
    client = FakeClient({}, [(1, "Project Alpha")])
    with pytest.raises(TelegramMCPError, match=r"Project Alpha \(id 1\)"):
        await resolve_chat(client, "alpha", POLICY)


async def test_resolve_username_goes_straight_to_telegram() -> None:
    client = FakeClient({"@durov": "peer:durov"}, [])
    assert await resolve_chat(client, "@durov", POLICY) == "peer:durov"
    with pytest.raises(TelegramMCPError, match="Cannot resolve '@nobody'"):
        await resolve_chat(client, "@nobody", POLICY)
