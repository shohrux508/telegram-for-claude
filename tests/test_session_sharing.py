"""Several processes share one Telethon session file: none may hold its write lock."""

import sqlite3
import subprocess
import sys
from pathlib import Path

from telethon import types

from telegram_mcp.accounts import SharedSQLiteSession

WRITER = """
import sys, time
from telethon import types
from telegram_mcp.accounts import SharedSQLiteSession
session = SharedSQLiteSession(sys.argv[1])
session.process_entities(types.contacts.ResolvedPeer(
    peer=types.PeerUser(1), chats=[], users=[types.User(id=1, access_hash=1, first_name="A")]))
print("written", flush=True)
time.sleep(30)  # stays connected, like a running MCP server
"""


def _entity(user_id: int) -> types.contacts.ResolvedPeer:
    user = types.User(id=user_id, access_hash=user_id, first_name="U")
    return types.contacts.ResolvedPeer(peer=types.PeerUser(user_id), chats=[], users=[user])


def test_entities_are_committed_at_once(tmp_path: Path) -> None:
    session = SharedSQLiteSession(str(tmp_path / "a.session"))
    session.process_entities(_entity(5))
    assert session._conn is not None and not session._conn.in_transaction
    with sqlite3.connect(tmp_path / "a.session") as other:
        assert other.execute("SELECT id FROM entities").fetchall() == [(5,)]


def test_a_second_process_can_write_while_the_first_stays_connected(tmp_path: Path) -> None:
    path = tmp_path / "shared.session"
    SharedSQLiteSession(str(path)).close()  # create the file and its tables
    holder = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(path)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "written"
        mine = SharedSQLiteSession(str(path))
        mine.process_entities(_entity(2))  # stock Telethon fails here: database is locked
        assert mine.get_input_entity(1) is not None
        mine.close()
    finally:
        holder.kill()
        holder.wait()
