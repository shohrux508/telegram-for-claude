"""Starts the settings page and opens it in the browser.

Only one copy runs: starting the program again just reopens the page of the
one already running.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import webbrowser

import uvicorn

from telegram_mcp.background import exclusive_lock
from telegram_mcp.config import Settings, data_dir_from_env
from telegram_mcp.log import configure_logging


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _hide_own_console() -> None:
    """Hide the black window Windows opens for a double-clicked program.

    Only when the console is ours alone: if it belongs to a terminal the user
    started us from, it stays.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    processes = (ctypes.c_uint * 4)()
    if kernel32.GetConsoleProcessList(processes, 4) == 1:
        ctypes.windll.user32.ShowWindow(kernel32.GetConsoleWindow(), 0)


async def _serve(port: int) -> None:
    from telegram_mcp.app.web import SettingsApp, build_app, watch_idle

    app = SettingsApp(Settings, port)
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    (data_dir_from_env() / "settings-page.json").write_text(json.dumps({"url": url}))
    config = uvicorn.Config(
        build_app(app), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    watcher = asyncio.create_task(watch_idle(app))
    while not server.started and not serving.done():  # noqa: ASYNC110 - uvicorn has no event
        await asyncio.sleep(0.05)
    if not os.environ.get("TELEGRAM_MCP_NO_BROWSER"):  # set by tests and screenshots
        webbrowser.open(url)
    print(f"Настройки открыты в браузере: {url}")
    print("Закройте вкладку или нажмите «Готово» на странице, чтобы выйти.")
    stop = asyncio.create_task(app.stop.wait())
    await asyncio.wait({serving, stop}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    watcher.cancel()
    await serving


def run_settings() -> None:
    data_dir = data_dir_from_env()
    data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging("WARNING")
    with exclusive_lock(data_dir / "settings-page.lock") as acquired:
        if not acquired:
            try:
                url = json.loads((data_dir / "settings-page.json").read_text())["url"]
                webbrowser.open(url)
            except (OSError, ValueError, KeyError):
                print("Настройки уже открыты в другом окне.")
            return
        _hide_own_console()
        asyncio.run(_serve(_free_port()))
