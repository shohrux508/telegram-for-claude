"""Command line entry point: run the MCP server or manage logged-in accounts."""

from __future__ import annotations

import argparse
import asyncio
import io
import sys

from pydantic import ValidationError

from telegram_mcp.accounts import AccountManager, TelegramMCPError, resolve_chat
from telegram_mcp.config import Settings
from telegram_mcp.formatting import format_entity
from telegram_mcp.log import configure_logging
from telegram_mcp.policy import ChatPolicy
from telegram_mcp.server import make_archive
from telegram_mcp.sync import sync_account


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing = ", ".join(str(error["loc"][0]) for error in exc.errors())
        sys.exit(
            f"Configuration error in: {missing}.\n"
            "Copy .env.example to .env and set TELEGRAM_API_ID and TELEGRAM_API_HASH "
            "(get them at https://my.telegram.org -> API development tools)."
        )


async def _login(manager: AccountManager, account: str) -> None:
    client = manager.build_client(account)
    # Prompts for the phone number, the login code and the 2FA password in this
    # terminal. They go straight to Telegram and are never stored or logged.
    await client.start()
    try:
        me = await client.get_me()
        print(f"\nLogged in as {format_entity(me)}")
        print(f"Account name: {account}")
        print(f"Session file: {manager.session_path(account)}")
        print("Keep that file private: whoever has it has full access to the account.")
    finally:
        await client.disconnect()


async def _accounts(manager: AccountManager) -> None:
    names = manager.list_accounts()
    if not names:
        print("No accounts yet. Run: uv run telegram-mcp login")
        return
    try:
        for name in names:
            try:
                client = await manager.get(name)
                print(f"{name}: {format_entity(await client.get_me())}")
            except TelegramMCPError as exc:
                print(f"{name}: {exc}")
    finally:
        await manager.close()


async def _logout(manager: AccountManager, account: str) -> None:
    if not manager.session_path(account).exists():
        sys.exit(f"No such account: {account}")
    client = manager.build_client(account)
    await client.connect()
    # Revokes the session on Telegram's side and deletes the local session file.
    await client.log_out()
    print(f"Logged out and removed account {account}.")


async def _sync(manager: AccountManager, settings: Settings, args: argparse.Namespace) -> None:
    if not settings.archive_enabled:
        sys.exit(
            "The archive is turned off. Turn it on in the settings (telegram-mcp settings), "
            "or set TELEGRAM_MCP_CORE_URL and TELEGRAM_MCP_CORE_TOKEN in .env."
        )
    policy = ChatPolicy(settings.hidden_chats)
    archive = make_archive(settings)
    assert archive is not None
    backfill = args.backfill
    try:
        while True:
            try:
                client = await manager.get(args.account)
                only_chat_id = None
                if args.chat:
                    peer = await resolve_chat(client, args.chat, policy)
                    only_chat_id = await client.get_peer_id(peer)
                report = await sync_account(
                    client,
                    archive,
                    policy,
                    initial_limit=settings.sync_initial_limit,
                    backfill=backfill,
                    only_chat_id=only_chat_id,
                    # Unattended, so waiting out a long rate limit beats giving up.
                    max_flood_wait=900,
                    progress=print,
                )
                print(report.summary())
            except (TelegramMCPError, ConnectionError, OSError) as exc:
                if not args.watch:
                    raise
                # A laptop sleeps and changes networks; the next round simply resumes.
                print(f"Sync round failed, will retry: {exc}")
            if not args.watch:
                break
            backfill = 0  # older history is a one-off request, not something to repeat
            await asyncio.sleep(args.interval)
    finally:
        await archive.aclose()
        await manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="telegram-mcp",
        description="Local MCP server that gives Claude access to your Telegram accounts.",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the MCP server over stdio (default)")
    login = commands.add_parser("login", help="log an account in interactively")
    login.add_argument(
        "--account", help="name for this account (default: TELEGRAM_MCP_DEFAULT_ACCOUNT)"
    )
    commands.add_parser("accounts", help="list logged-in accounts")
    logout = commands.add_parser("logout", help="revoke a session and delete it")
    logout.add_argument("--account", required=True)
    sync = commands.add_parser("sync", help="copy new messages into the history archive")
    sync.add_argument("--account", help="account to sync (default: the default account)")
    sync.add_argument("--chat", help="only this chat: id, @username or exact title")
    sync.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="also fetch N older messages per chat, below what is already archived",
    )
    sync.add_argument("--watch", action="store_true", help="keep running and sync periodically")
    sync.add_argument(
        "--interval", type=int, default=300, metavar="SEC", help="pause between --watch rounds"
    )
    args = parser.parse_args()

    settings = _load_settings()
    configure_logging(settings.log_level)
    if isinstance(sys.stdout, io.TextIOWrapper):
        # Chat names contain emoji; a legacy Windows console must not crash on them.
        sys.stdout.reconfigure(errors="replace")

    if args.command in (None, "serve"):
        # Imported here so `login` does not pay for loading the MCP SDK.
        from telegram_mcp.server import build_server

        build_server(settings).run("stdio")
        return

    manager = AccountManager(settings)
    try:
        if args.command == "login":
            asyncio.run(_login(manager, args.account or settings.default_account))
        elif args.command == "accounts":
            asyncio.run(_accounts(manager))
        elif args.command == "logout":
            asyncio.run(_logout(manager, args.account))
        elif args.command == "sync":
            asyncio.run(_sync(manager, settings, args))
    except TelegramMCPError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
