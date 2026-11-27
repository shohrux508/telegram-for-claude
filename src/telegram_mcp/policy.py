"""Which chats the server refuses to expose, to Claude or to the archive."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from telethon import utils

from telegram_mcp.accounts import TelegramMCPError

# Telegram's own service accounts. They receive login codes and codes for
# third-party services: reading them is enough to take over the account, so
# they are hidden unconditionally and cannot be re-enabled by configuration.
SYSTEM_HIDDEN_CHATS = frozenset(
    {
        777000,  # "Telegram": login codes, new-device alerts
        489000,  # "Verification Codes": codes sent by other services through Telegram
    }
)


class ChatPolicy:
    """Decides whether a chat may be read, written to, searched or archived."""

    def __init__(self, extra_hidden: Iterable[int] = ()) -> None:
        self._hidden = SYSTEM_HIDDEN_CHATS | frozenset(extra_hidden)

    def is_hidden(self, chat_id: int | None) -> bool:
        return chat_id in self._hidden

    def ensure_visible(self, peer: Any) -> None:
        """Raise if the resolved peer is a hidden chat."""
        try:
            chat_id = utils.get_peer_id(peer)
        except TypeError:
            return  # InputPeerSelf: Saved Messages has no id here and is never hidden
        if self.is_hidden(chat_id):
            raise TelegramMCPError(
                f"Chat {chat_id} is hidden by server policy and cannot be read or written. "
                "Do not try other ways to reach it."
            )
