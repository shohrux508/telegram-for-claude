"""Keeps the history archive fresh while Claude has the server running.

Claude starts one server process per window or session, so several may run at
once. An OS file lock makes sure only one of them syncs at a time; the lock is
released by the OS if that process dies, so it can never get stuck.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

import structlog

from telegram_mcp.accounts import AccountManager, TelegramMCPError
from telegram_mcp.archive import Archive, ArchiveError
from telegram_mcp.config import Settings
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.sync import sync_account

log = structlog.get_logger(__name__)

# Let Claude finish connecting before the first round competes for the network.
_FIRST_ROUND_DELAY_S = 20.0
# Unattended rounds may wait out short rate limits; longer ones end the round.
_MAX_FLOOD_WAIT_S = 120


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    """Yield True if this process got the lock, False if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: IO[bytes] = open(path, "a+b")  # noqa: SIM115 - released in finally
    acquired = False
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


async def sync_round(
    manager: AccountManager, archive: Archive, settings: Settings, policy: ChatPolicy
) -> None:
    """One pass over every logged-in account, if no other process is already syncing."""
    with exclusive_lock(settings.data_dir / "sync.lock") as acquired:
        if not acquired:
            log.debug("background_sync.skipped_other_process", pid=os.getpid())
            return
        for account in manager.list_accounts():
            try:
                client = await manager.get(account)
                report = await sync_account(
                    client,
                    archive,
                    policy,
                    initial_limit=settings.sync_initial_limit,
                    max_flood_wait=_MAX_FLOOD_WAIT_S,
                )
                log.info(
                    "background_sync.done",
                    account=account,
                    new=report.new_messages,
                    complete=report.complete,
                )
            except (TelegramMCPError, ArchiveError, ConnectionError, OSError) as exc:
                log.warning("background_sync.failed", account=account, error=str(exc))


async def run_forever(
    manager: AccountManager, archive: Archive, settings: Settings, policy: ChatPolicy
) -> None:
    await asyncio.sleep(_FIRST_ROUND_DELAY_S)
    while True:
        try:
            await sync_round(manager, archive, settings, policy)
        except Exception:
            # The archive is a convenience: a bug here must never take the server down.
            log.exception("background_sync.crashed")
        await asyncio.sleep(settings.sync_interval_minutes * 60)
