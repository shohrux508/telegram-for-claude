"""Telegram account sessions: one Telethon client per logged-in account."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from telethon import TelegramClient

from telegram_mcp import __version__
from telegram_mcp.config import Settings

if TYPE_CHECKING:
    from telegram_mcp.policy import ChatPolicy

log = structlog.get_logger(__name__)

_ACCOUNT_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
_SESSION_SUFFIX = ".session"
_NUMERIC_ID = re.compile(r"-?\d+")

# Above this Telethon raises FloodWaitError instead of silently sleeping, so a
# tool call never hangs for minutes without the model knowing why.
_FLOOD_SLEEP_THRESHOLD = 10


class TelegramMCPError(Exception):
    """An anticipated failure whose message is safe and useful to show to the model."""


def validate_account_name(name: str) -> str:
    if not _ACCOUNT_NAME.fullmatch(name):
        raise TelegramMCPError(
            f"Invalid account name {name!r}: use 1-32 latin letters, digits, '_' or '-'."
        )
    return name


def parse_chat_ref(chat: str | int) -> str | int:
    """Normalize a chat reference: numeric strings become ids, the rest stays a string.

    A phone number must keep its leading '+', otherwise it is read as a numeric id.
    """
    if isinstance(chat, int):
        return chat
    ref = chat.strip()
    if not ref:
        raise TelegramMCPError("Chat reference is empty.")
    if _NUMERIC_ID.fullmatch(ref):
        return int(ref)
    return ref


class AccountManager:
    """Owns the Telethon clients and connects them lazily, on first use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def session_path(self, account: str) -> Path:
        return self._settings.sessions_dir / f"{validate_account_name(account)}{_SESSION_SUFFIX}"

    def list_accounts(self) -> list[str]:
        sessions_dir = self._settings.sessions_dir
        if not sessions_dir.is_dir():
            return []
        return sorted(path.stem for path in sessions_dir.glob(f"*{_SESSION_SUFFIX}"))

    def resolve_account_name(self, account: str | None) -> str:
        """Pick the account to use: explicit name, the default, or the only one logged in."""
        if account:
            return validate_account_name(account)
        accounts = self.list_accounts()
        default = self._settings.default_account
        if default in accounts or not accounts:
            return default
        if len(accounts) == 1:
            return accounts[0]
        raise TelegramMCPError(
            f"Several accounts are logged in ({', '.join(accounts)}) and none is named "
            f"{default!r}: pass the `account` argument explicitly."
        )

    def build_client(self, account: str) -> Any:
        """Create a client without connecting it. Must be called inside a running loop."""
        path = self.session_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        return TelegramClient(
            str(path),
            self._settings.api_id,
            self._settings.api_hash.get_secret_value(),
            # The server only issues requests; skipping the update stream keeps it idle.
            receive_updates=False,
            flood_sleep_threshold=_FLOOD_SLEEP_THRESHOLD,
            device_model="telegram-mcp",
            app_version=__version__,
        )

    async def get(self, account: str | None = None) -> Any:
        """Return a connected, authorized client for the account."""
        name = self.resolve_account_name(account)
        async with self._lock:
            client = self._clients.get(name)
            if client is None:
                client = await self._connect(name)
                self._clients[name] = client
            elif not client.is_connected():
                await client.connect()
            return client

    async def _connect(self, name: str) -> Any:
        if not self.session_path(name).exists():
            raise TelegramMCPError(_login_hint(name, "is not logged in"))
        client = self.build_client(name)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramMCPError(_login_hint(name, "has an expired or revoked session"))
        log.info("account_connected", account=name)
        return client

    async def close(self) -> None:
        async with self._lock:
            clients, self._clients = self._clients, {}
        for name, client in clients.items():
            try:
                await client.disconnect()
            except Exception:
                log.warning("account_disconnect_failed", account=name, exc_info=True)


def _login_hint(account: str, problem: str) -> str:
    return (
        f"Telegram account {account!r} {problem}. The user must run "
        f"`uv run telegram-mcp login --account {account}` in a terminal in the project folder."
    )


def _is_address(ref: str) -> bool:
    """True for references that name a peer directly rather than by dialog title."""
    lowered = ref.lower()
    return (
        lowered in ("me", "self")
        or ref.startswith(("@", "+"))
        or "t.me/" in lowered
        or "telegram.me/" in lowered
        or "telegram.dog/" in lowered
    )


async def resolve_chat(client: Any, chat: str | int, policy: ChatPolicy) -> Any:
    """Turn a chat reference into a Telethon input peer, refusing hidden chats.

    Accepts a numeric id, @username, t.me link, +phone, "me", or a dialog title.
    Every tool reaches a chat through here, which is what makes the policy binding.
    """
    peer = await _resolve_peer(client, chat)
    policy.ensure_visible(peer)
    return peer


async def _resolve_peer(client: Any, chat: str | int) -> Any:
    ref = parse_chat_ref(chat)

    if isinstance(ref, int):
        try:
            return await client.get_input_entity(ref)
        except ValueError:
            # Unknown to the session cache: loading the dialog list fills the
            # cache with every chat the account is in.
            await client.get_dialogs(limit=None)
        try:
            return await client.get_input_entity(ref)
        except ValueError as exc:
            raise TelegramMCPError(
                f"No chat with id {ref} is known to this account. "
                "Use list_chats or find_chats to get a valid id."
            ) from exc

    if _is_address(ref):
        try:
            return await client.get_input_entity(ref)
        except ValueError as exc:
            raise TelegramMCPError(f"Cannot resolve {ref!r}: {exc}") from exc

    # Anything else is a dialog title. It is never tried as a public username:
    # a chat titled "Family" must not resolve to whoever owns @family.
    dialogs = await client.get_dialogs(limit=None)
    needle = ref.casefold()
    exact = [d for d in dialogs if (d.name or "").casefold() == needle]
    if len(exact) == 1:
        return exact[0].input_entity
    candidates = exact or [d for d in dialogs if needle in (d.name or "").casefold()]
    if not candidates:
        raise TelegramMCPError(
            f"Cannot find a chat matching {ref!r}. Use list_chats or find_chats to look it up."
        )
    listed = "; ".join(f"{d.name} (id {d.id})" for d in candidates[:15])
    raise TelegramMCPError(f"{ref!r} is ambiguous, pass the numeric id instead. Matches: {listed}")
