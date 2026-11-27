"""HTTP client for the message archive that lives on the core server.

The laptop only pushes messages and asks questions; storage and search are the
server's job. The JSON shapes are defined by core's `modules/tg_archive/models.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2

from telegram_mcp.accounts import TelegramMCPError

_TIMEOUT_S = 60.0


class ArchiveError(TelegramMCPError):
    """The archive server could not be reached or refused the request."""


@dataclass(frozen=True)
class Cursor:
    """The id range of one chat that the server already holds."""

    oldest_id: int
    newest_id: int
    count: int


@dataclass(frozen=True)
class SyncState:
    cursors: dict[int, Cursor]
    selected: frozenset[int]


class CoreArchive:
    """Thin wrapper over core's /api/tg-archive endpoints."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._http = httpx2.AsyncClient(
            base_url=f"{base_url}/api/tg-archive",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_S,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise ArchiveError(
                f"Cannot reach the archive server at {self._base_url}: {exc!r}"
            ) from exc
        if response.status_code == 401:
            raise ArchiveError(
                "The archive server rejected the token: check TELEGRAM_MCP_CORE_TOKEN in .env."
            )
        if response.status_code == 404:
            raise ArchiveError(
                f"{self._base_url} has no archive API: the tg_archive module is not deployed "
                "there yet, or TELEGRAM_MCP_CORE_URL points at the wrong server."
            )
        if response.is_error:
            raise ArchiveError(
                f"Archive server error {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    async def sync_state(self, account_id: int) -> SyncState:
        data = await self._request("GET", "/sync-state", params={"account_id": account_id})
        return SyncState(
            cursors={
                item["chat_id"]: Cursor(item["oldest_id"], item["newest_id"], item["count"])
                for item in data["cursors"]
            },
            selected=frozenset(data["selected"]),
        )

    async def ingest(
        self, account_id: int, chats: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Push a batch; returns (new, updated) message counts."""
        data = await self._request(
            "POST",
            "/ingest",
            json={"account_id": account_id, "chats": chats, "messages": messages},
        )
        return data["new_messages"], data["updated_messages"]

    async def set_selection(
        self,
        account_id: int,
        chat_id: int,
        *,
        selected: bool,
        title: str = "",
        kind: str = "",
        purge: bool = False,
    ) -> int:
        """Opt a chat in or out; returns how many stored messages were purged."""
        data = await self._request(
            "POST",
            "/selection",
            json={
                "account_id": account_id,
                "chat_id": chat_id,
                "selected": selected,
                "title": title,
                "kind": kind,
                "purge": purge,
            },
        )
        return int(data["purged_messages"])

    async def search(self, **params: Any) -> list[dict[str, Any]]:
        present = {key: value for key, value in params.items() if value is not None}
        data = await self._request("GET", "/search", params=present)
        hits: list[dict[str, Any]] = data["hits"]
        return hits

    async def chats(self, account_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", "/chats", params={"account_id": account_id})
        chats: list[dict[str, Any]] = data["chats"]
        return chats
