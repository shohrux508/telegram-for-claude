"""The settings page server: Starlette on 127.0.0.1, opened in the user's browser.

Any website the user visits can send requests to localhost, so every API call
must carry a random per-run token in a custom header (which a cross-site page
cannot set without a CORS preflight this server never answers), and the Host
header must be this exact address (against DNS rebinding).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from typing import Any

import qrcode
import qrcode.image.svg
import structlog
from dotenv import dotenv_values
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from telethon import utils

from telegram_mcp import __version__
from telegram_mcp.accounts import AccountManager, TelegramMCPError, validate_account_name
from telegram_mcp.app import claude_config
from telegram_mcp.app.login import QrLogin
from telegram_mcp.config import EDITABLE_KEYS, Settings, read_settings_file, write_settings_file
from telegram_mcp.formatting import display_name, entity_kind
from telegram_mcp.policy import SYSTEM_HIDDEN_CHATS, ChatPolicy
from telegram_mcp.server import make_archive
from telegram_mcp.sync import ALWAYS_ARCHIVED_KIND, sync_account

log = structlog.get_logger(__name__)

# The page pings every few seconds; without pings for this long the app exits,
# so closing the browser tab is enough to close the program.
IDLE_EXIT_S = 90.0
_MAX_CHATS_LISTED = 300
# Keys whose current value is shown to the page; secrets are reported as set/unset.
_SECRET_KEYS = frozenset({"api_hash", "core_token"})


class SettingsApp:
    def __init__(self, settings_loader: Callable[[], Settings], port: int) -> None:
        self._load = settings_loader
        self.settings = settings_loader()
        self.manager = AccountManager(self.settings)
        self.login = QrLogin(self.manager)
        self.token = secrets.token_urlsafe(24)
        self.port = port
        self.last_ping = time.monotonic()
        self.pinged = False
        self.stop = asyncio.Event()
        self._sync_lock = asyncio.Lock()

    # -- helpers --------------------------------------------------------------

    async def reload(self) -> None:
        """Pick up saved settings: new manager, so a new API ID takes effect."""
        await self.login.cancel()
        await self.manager.close()
        self.settings = self._load()
        self.manager = AccountManager(self.settings)
        self.login = QrLogin(self.manager)

    def overridden(self) -> list[str]:
        """Settings fixed by the environment or .env: saving them here would change nothing."""
        names: set[str] = set(os.environ)
        env_files = Settings.model_config.get("env_file") or ()
        for env_file in env_files if isinstance(env_files, tuple) else (env_files,):
            with contextlib.suppress(OSError):
                names.update(dotenv_values(env_file))
        upper = {name.upper() for name in names}
        aliases = {"api_id": "TELEGRAM_API_ID", "api_hash": "TELEGRAM_API_HASH"}
        return sorted(
            key for key in EDITABLE_KEYS if aliases.get(key, f"TELEGRAM_MCP_{key.upper()}") in upper
        )

    async def accounts(self) -> list[dict[str, Any]]:
        result = []
        for name in self.manager.list_accounts():
            try:
                client = await self.manager.get(name)
                me = await client.get_me()
                result.append({"name": name, "ok": True, "who": display_name(me), "id": me.id})
            except (TelegramMCPError, ConnectionError, OSError) as exc:
                result.append({"name": name, "ok": False, "error": str(exc)})
        return result

    # -- endpoints --------------------------------------------------------------

    async def page(self, request: Request) -> Response:
        html = resources.files("telegram_mcp.app").joinpath("page.html").read_text("utf-8")
        return HTMLResponse(
            html.replace("__TOKEN__", self.token).replace("__VERSION__", __version__)
        )

    async def state(self, request: Request) -> Response:
        s = self.settings
        values: dict[str, Any] = {}
        for key in sorted(EDITABLE_KEYS):
            value = getattr(s, key)
            if key in _SECRET_KEYS:
                value = bool(value.get_secret_value())
            values[key] = value
        values["api_id"] = s.api_id if "api_id" in read_settings_file() else 0
        archive_info: dict[str, Any] = {"enabled": s.archive_enabled}
        accounts = await self.accounts()
        archive = make_archive(s)
        if archive is not None and accounts:
            try:
                total = chats = 0
                for account in accounts:
                    if account.get("ok"):
                        rows = await archive.chats(account["id"])
                        chats += len(rows)
                        total += sum(row["messages"] for row in rows)
                archive_info.update(messages=total, chats=chats)
            except Exception as exc:
                archive_info["error"] = str(exc)
            finally:
                await archive.aclose()
        code = await asyncio.to_thread(claude_config.code_registered)
        return JSONResponse(
            {
                "version": __version__,
                "api_configured": s.api_configured,
                "accounts": accounts,
                "settings": values,
                "overridden": self.overridden(),
                "archive": archive_info,
                "login": vars(self.login.state),
                "claude": {
                    "desktop": [
                        {"path": str(t.path), "exists": t.exists, "registered": t.registered}
                        for t in claude_config.desktop_status()
                    ],
                    "code": code,
                },
            }
        )

    async def ping(self, request: Request) -> Response:
        self.last_ping = time.monotonic()
        self.pinged = True
        return JSONResponse({"ok": True})

    async def login_start(self, request: Request) -> Response:
        body = await request.json()
        account = validate_account_name(str(body.get("account") or self.settings.default_account))
        state = await self.login.start(account)
        return JSONResponse(vars(state))

    async def login_status(self, request: Request) -> Response:
        return JSONResponse(vars(self.login.state))

    async def login_qr(self, request: Request) -> Response:
        url = self.login.state.qr_url
        if not url:
            return Response(status_code=404)
        buffer = io.BytesIO()
        qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12).save(buffer)
        return Response(
            buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"}
        )

    async def login_password(self, request: Request) -> Response:
        body = await request.json()
        state = await self.login.submit_password(str(body.get("password") or ""))
        return JSONResponse(vars(state))

    async def login_cancel(self, request: Request) -> Response:
        await self.login.cancel()
        return JSONResponse(vars(self.login.state))

    async def logout(self, request: Request) -> Response:
        body = await request.json()
        account = validate_account_name(str(body.get("account")))
        client = await self.manager.get(account)
        await client.log_out()  # revokes the session in Telegram and deletes the file
        await self.manager.close()
        self.manager = AccountManager(self.settings)
        self.login = QrLogin(self.manager)
        return JSONResponse({"ok": True})

    async def save_settings(self, request: Request) -> Response:
        body = await request.json()
        changes = {key: value for key, value in body.items() if key in EDITABLE_KEYS}
        # A blank secret field means "keep what is saved", not "erase it".
        for key in _SECRET_KEYS:
            if key in changes and not changes[key]:
                del changes[key]
        # Validate the merged result before writing anything.
        Settings.model_validate(
            {**read_settings_file(), **changes, "data_dir": self.settings.data_dir}
        )
        write_settings_file(changes)
        await self.reload()
        return JSONResponse({"ok": True})

    async def chats(self, request: Request) -> Response:
        account = validate_account_name(
            request.query_params.get("account") or self.settings.default_account
        )
        client = await self.manager.get(account)
        me = await client.get_me()
        selected: frozenset[int] = frozenset()
        archive = make_archive(self.settings)
        if archive is not None:
            try:
                selected = (await archive.sync_state(me.id)).selected
            finally:
                await archive.aclose()
        hidden = set(self.settings.hidden_chats)
        rows = []
        async for dialog in client.iter_dialogs(limit=_MAX_CHATS_LISTED, ignore_migrated=True):
            if dialog.id in SYSTEM_HIDDEN_CHATS:
                continue
            kind = entity_kind(dialog.entity)
            rows.append(
                {
                    "id": dialog.id,
                    "title": dialog.name or "(без названия)",
                    "kind": kind,
                    "hidden": dialog.id in hidden,
                    "archived": kind == ALWAYS_ARCHIVED_KIND or dialog.id in selected,
                    "always_archived": kind == ALWAYS_ARCHIVED_KIND,
                }
            )
        return JSONResponse({"chats": rows})

    async def save_chats(self, request: Request) -> Response:
        """Body: {account, hidden: [ids], archive: [ids of non-personal chats to keep]}."""
        body = await request.json()
        account = validate_account_name(str(body.get("account") or self.settings.default_account))
        hidden = sorted({int(i) for i in body.get("hidden", [])})
        wanted = {int(i) for i in body.get("archive", [])}
        write_settings_file({"hidden_chats": hidden})
        # Only the chat lists changed: keep the connected clients.
        self.settings = self._load()
        archive = make_archive(self.settings)
        if archive is not None:
            client = await self.manager.get(account)
            me = await client.get_me()
            try:
                current = (await archive.sync_state(me.id)).selected
                for chat_id in wanted - current:
                    entity = await client.get_entity(chat_id)
                    await archive.set_selection(
                        me.id,
                        utils.get_peer_id(entity),
                        selected=True,
                        title=display_name(entity),
                        kind=entity_kind(entity),
                    )
                for chat_id in current - wanted:
                    await archive.set_selection(me.id, chat_id, selected=False, purge=True)
                # Hiding a chat also removes its copy: hidden means Claude never sees it.
                for chat_id in hidden:
                    await archive.set_selection(me.id, chat_id, selected=False, purge=True)
            finally:
                await archive.aclose()
        return JSONResponse({"ok": True})

    async def connect_claude(self, request: Request) -> Response:
        desktop = await asyncio.to_thread(claude_config.register_desktop)
        try:
            code = await asyncio.to_thread(claude_config.register_code)
            code_error = ""
        except (RuntimeError, OSError) as exc:
            code, code_error = False, str(exc)
        return JSONResponse(
            {"desktop": [str(p) for p in desktop], "code": code, "code_error": code_error}
        )

    async def sync_now(self, request: Request) -> Response:
        archive = make_archive(self.settings)
        if archive is None:
            return JSONResponse({"summary": "Архив выключен."})
        policy = ChatPolicy(self.settings.hidden_chats)
        lines = []
        async with self._sync_lock:
            try:
                for name in self.manager.list_accounts():
                    client = await self.manager.get(name)
                    report = await sync_account(
                        client,
                        archive,
                        policy,
                        initial_limit=self.settings.sync_initial_limit,
                        max_seconds=120,
                        max_flood_wait=30,
                    )
                    done = "" if report.complete else " (не всё — продолжится в фоне)"
                    lines.append(f"{name}: +{report.new_messages} сообщений{done}")
            finally:
                await archive.aclose()
        return JSONResponse({"summary": "; ".join(lines) or "Нет аккаунтов."})

    async def quit(self, request: Request) -> Response:
        self.stop.set()
        return JSONResponse({"ok": True})


def _guarded(app: SettingsApp, handler: Callable[[Request], Awaitable[Response]]) -> Any:
    async def endpoint(request: Request) -> Response:
        if request.headers.get("host") != f"127.0.0.1:{app.port}":
            return Response("Forbidden", status_code=403)
        is_page = request.url.path == "/"
        given = request.query_params.get("t") if is_page else request.headers.get("x-token")
        if not given or not secrets.compare_digest(given, app.token):
            return Response("Forbidden", status_code=403)
        try:
            return await handler(request)
        except (TelegramMCPError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.exception("settings.request_failed", path=request.url.path)
            return JSONResponse({"error": f"Ошибка: {exc}"}, status_code=500)

    return endpoint


def build_app(app: SettingsApp) -> Starlette:
    routes = [
        ("/", app.page, ["GET"]),
        ("/api/state", app.state, ["GET"]),
        ("/api/ping", app.ping, ["POST"]),
        ("/api/login/start", app.login_start, ["POST"]),
        ("/api/login/status", app.login_status, ["GET"]),
        ("/api/login/qr", app.login_qr, ["GET"]),
        ("/api/login/password", app.login_password, ["POST"]),
        ("/api/login/cancel", app.login_cancel, ["POST"]),
        ("/api/logout", app.logout, ["POST"]),
        ("/api/settings", app.save_settings, ["POST"]),
        ("/api/chats", app.chats, ["GET"]),
        ("/api/chats", app.save_chats, ["POST"]),
        ("/api/claude/connect", app.connect_claude, ["POST"]),
        ("/api/sync", app.sync_now, ["POST"]),
        ("/api/quit", app.quit, ["POST"]),
    ]
    # Starlette matches the first route by path, so GET and POST /api/chats share one.
    by_path: dict[str, dict[str, Any]] = {}
    for path, handler, methods in routes:
        for method in methods:
            by_path.setdefault(path, {})[method] = _guarded(app, handler)

    def dispatcher(handlers: dict[str, Any]) -> Any:
        async def endpoint(request: Request) -> Response:
            handler = handlers.get(request.method)
            if handler is None:
                return Response(status_code=405)
            response: Response = await handler(request)
            return response

        return endpoint

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await app.login.cancel()
            await app.manager.close()

    return Starlette(
        routes=[
            Route(path, dispatcher(handlers), methods=list(handlers))
            for path, handlers in by_path.items()
        ],
        lifespan=lifespan,
    )


async def watch_idle(app: SettingsApp) -> None:
    """Exit when the page has gone quiet (tab closed) or the user pressed Done."""
    while not app.stop.is_set():
        await asyncio.sleep(5)
        # Before the first ping the browser may still be starting: allow longer.
        limit = IDLE_EXIT_S if app.pinged else IDLE_EXIT_S * 4
        if time.monotonic() - app.last_ping > limit:
            app.stop.set()
