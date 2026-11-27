import subprocess
import sys
import time
from pathlib import Path

from fakes import AZIZ, FakeManager, FakeTelegram

from telegram_mcp import background
from telegram_mcp.config import Settings
from telegram_mcp.local_archive import LocalArchive
from telegram_mcp.policy import ChatPolicy

HOLD_LOCK = """
import sys, time
from pathlib import Path
from telegram_mcp.background import exclusive_lock
with exclusive_lock(Path(sys.argv[1])) as got:
    print("got" if got else "busy", flush=True)
    time.sleep(30)
"""


def test_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(lock)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "got"
        with background.exclusive_lock(lock) as acquired:
            assert not acquired
    finally:
        holder.kill()
        holder.wait()
    # The OS releases it when the holder dies, so nothing can get stuck. Windows
    # does that asynchronously after the process ends, hence the short wait.
    deadline = time.monotonic() + 10
    while True:
        with background.exclusive_lock(lock) as acquired:
            if acquired or time.monotonic() > deadline:
                break
        time.sleep(0.2)
    assert acquired


class Accounts(FakeManager):
    def list_accounts(self) -> list[str]:
        return ["main"]


async def test_round_syncs_every_account_and_skips_when_locked(settings: Settings) -> None:
    telegram = FakeTelegram({AZIZ: [1, 2, 3]})
    archive = LocalArchive(settings.data_dir / "archive.db")
    manager = Accounts(telegram)

    with background.exclusive_lock(settings.data_dir / "sync.lock"):
        await background.sync_round(manager, archive, settings, ChatPolicy())  # type: ignore[arg-type]
    assert (await archive.sync_state(1000)).cursors == {}

    await background.sync_round(manager, archive, settings, ChatPolicy())  # type: ignore[arg-type]
    assert (await archive.sync_state(1000)).cursors[AZIZ].count == 3
