"""Logging an account in by QR code, driven step by step from the settings page.

The phone does the work: the user scans the code in Telegram → Settings →
Devices → Link Desktop Device. Nobody types a phone number or a login code, and
the cloud password, if one is set, goes straight from the page to Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

from telegram_mcp.accounts import AccountManager, TelegramMCPError
from telegram_mcp.formatting import display_name

log = structlog.get_logger(__name__)

LoginStatus = Literal["idle", "waiting", "password", "done", "error"]
# Telegram's QR tokens live ~30 s; a new one is fetched this many times before giving up.
_MAX_QR_ROUNDS = 10


@dataclass
class LoginState:
    status: LoginStatus = "idle"
    account: str = ""
    qr_url: str = ""
    error: str = ""
    who: str = ""


class QrLogin:
    """One login at a time. The page polls `state` while the phone scans."""

    def __init__(self, manager: AccountManager) -> None:
        self._manager = manager
        self._client: Any = None
        self._task: asyncio.Task[None] | None = None
        self.state = LoginState()

    async def start(self, account: str) -> LoginState:
        await self.cancel()
        self.state = LoginState(status="waiting", account=account)
        try:
            client = self._manager.build_client(account)
            await client.connect()
        except (TelegramMCPError, ConnectionError, OSError) as exc:
            self.state = LoginState(status="error", account=account, error=str(exc))
            return self.state
        self._client = client
        if await client.is_user_authorized():
            await self._finish()
            return self.state
        self._task = asyncio.create_task(self._wait_for_scan())
        # Let the first QR code arrive before answering, so the page can show it at once.
        for _ in range(50):
            if self.state.qr_url or self.state.status != "waiting":
                break
            await asyncio.sleep(0.1)
        return self.state

    async def _wait_for_scan(self) -> None:
        client = self._client
        try:
            qr = await client.qr_login()
            for _ in range(_MAX_QR_ROUNDS):
                self.state.qr_url = qr.url
                try:
                    await qr.wait(timeout=25)
                    await self._finish()
                    return
                except TimeoutError:
                    await qr.recreate()
            self.state.status = "error"
            self.state.error = "Код так и не отсканировали. Нажмите «Войти» ещё раз."
        except SessionPasswordNeededError:
            self.state.status = "password"
            self.state.qr_url = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("login.qr_failed", error=repr(exc))
            self.state.status = "error"
            self.state.error = f"Telegram не принял вход: {exc}"

    async def submit_password(self, password: str) -> LoginState:
        if self.state.status != "password" or self._client is None:
            return self.state
        try:
            await self._client.sign_in(password=password)
        except PasswordHashInvalidError:
            self.state.error = "Пароль не подошёл. Это облачный пароль Telegram, не код из SMS."
            return self.state
        except Exception as exc:
            self.state.error = f"Telegram не принял пароль: {exc}"
            return self.state
        await self._finish()
        return self.state

    async def _finish(self) -> None:
        me = await self._client.get_me()
        self.state.status = "done"
        self.state.qr_url = ""
        self.state.error = ""
        self.state.who = display_name(me)
        log.info("login.done", account=self.state.account)
        # The settings page and Claude's servers reconnect with the saved session.
        await self._client.disconnect()
        self._client = None

    async def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
            self._client = None
        if self.state.status != "done":
            if self.state.account:
                # An abandoned login leaves an empty session file behind; it would show
                # up as a broken account everywhere else.
                with contextlib.suppress(OSError, TelegramMCPError):
                    self._manager.session_path(self.state.account).unlink(missing_ok=True)
            self.state = LoginState()
