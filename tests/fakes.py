"""In-memory stand-ins for Telegram and for the archive server on core."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx2
from telethon import types, utils
from telethon.tl.custom import Message

from telegram_mcp.archive import CoreArchive

ME = 1000
AZIZ = 5
SHOP_BOT = 6
TELEGRAM_SERVICE = 777000
WORK_GROUP = -1000000000123  # marked id of channel 123
WHEN = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

_ENTITIES: dict[int, Any] = {
    ME: types.User(id=ME, access_hash=1, first_name="Me", is_self=True),
    AZIZ: types.User(id=AZIZ, access_hash=1, first_name="Aziz"),
    SHOP_BOT: types.User(id=SHOP_BOT, access_hash=1, first_name="Shop", bot=True),
    TELEGRAM_SERVICE: types.User(id=TELEGRAM_SERVICE, access_hash=1, first_name="Telegram"),
    WORK_GROUP: types.Channel(
        id=123, access_hash=1, title="Work", megagroup=True, photo=types.ChatPhotoEmpty(), date=WHEN
    ),
}


class FakeTelegram:
    """A logged-in account with a fixed set of chats and a mutable message history."""

    def __init__(self, history: dict[int, list[int]]) -> None:
        self.history = {chat_id: sorted(ids) for chat_id, ids in history.items()}
        self.fetches: list[tuple[int, dict[str, Any]]] = []
        self.fail_with: Exception | None = None

    async def get_me(self) -> Any:
        return _ENTITIES[ME]

    async def iter_dialogs(self, **_: Any) -> AsyncIterator[SimpleNamespace]:
        for chat_id, ids in self.history.items():
            entity = _ENTITIES[chat_id]
            yield SimpleNamespace(
                id=chat_id,
                name=utils.get_display_name(entity),
                entity=entity,
                input_entity=chat_id,
                message=SimpleNamespace(id=ids[-1]) if ids else None,
            )

    async def iter_messages(
        self,
        entity: int,
        *,
        limit: int | None = None,
        min_id: int = 0,
        max_id: int = 0,
        reverse: bool = False,
    ) -> AsyncIterator[Message]:
        self.fetches.append((entity, {"min_id": min_id, "max_id": max_id, "reverse": reverse}))
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None  # fails once, like a rate limit
            raise error
        ids = [i for i in self.history[entity] if i > min_id and (not max_id or i < max_id)]
        ids.sort(reverse=not reverse)
        for message_id in ids[:limit]:
            yield Message(
                id=message_id,
                peer_id=types.PeerUser(AZIZ),
                date=WHEN + timedelta(minutes=message_id),
                message=f"message {message_id}",
                from_id=types.PeerUser(AZIZ),
            )

    # -- what the tools use to resolve a chat reference --

    async def get_input_entity(self, ref: Any) -> Any:
        if ref in _ENTITIES:
            return utils.get_input_peer(_ENTITIES[ref])
        raise ValueError(f"unknown {ref}")

    async def get_dialogs(self, limit: int | None = None) -> list[Any]:
        return []

    async def get_entity(self, peer: Any) -> Any:
        return _ENTITIES[utils.get_peer_id(peer)]

    async def get_peer_id(self, peer: Any) -> int:
        return int(utils.get_peer_id(peer))


class FakeManager:
    def __init__(self, client: FakeTelegram) -> None:
        self._client = client

    async def get(self, account: str | None = None) -> FakeTelegram:
        return self._client


class FakeCore:
    """Implements core's /api/tg-archive contract in memory, behind an httpx transport."""

    def __init__(self, token: str = "secret") -> None:
        self.token = token
        self.messages: dict[tuple[int, int], dict[str, Any]] = {}
        self.chats: dict[int, dict[str, Any]] = {}
        self.selected: list[int] = []
        self.batches: list[int] = []

    def archive(self, token: str | None = None) -> CoreArchive:
        return CoreArchive(
            "https://core.test",
            token if token is not None else self.token,
            transport=httpx2.MockTransport(self._handle),
        )

    def ids(self, chat_id: int) -> list[int]:
        return sorted(mid for cid, mid in self.messages if cid == chat_id)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx2.Response(401, json={"detail": "Unauthorized"})
        path = request.url.path.removeprefix("/api/tg-archive")
        body = json.loads(request.content) if request.content else {}
        if path == "/sync-state":
            cursors = [
                {"chat_id": cid, "oldest_id": ids[0], "newest_id": ids[-1], "count": len(ids)}
                for cid in {cid for cid, _ in self.messages}
                if (ids := self.ids(cid))
            ]
            return httpx2.Response(200, json={"cursors": cursors, "selected": self.selected})
        if path == "/ingest":
            self.batches.append(len(body["messages"]))
            new = 0
            for chat in body["chats"]:
                self.chats[chat["chat_id"]] = chat
            for message in body["messages"]:
                key = (message["chat_id"], message["message_id"])
                new += key not in self.messages
                self.messages[key] = message
            return httpx2.Response(
                200, json={"chats": len(body["chats"]), "new_messages": new, "updated_messages": 0}
            )
        if path == "/selection":
            chat_id, purged = body["chat_id"], 0
            self.selected = [cid for cid in self.selected if cid != chat_id]
            if body["selected"]:
                self.selected.append(chat_id)
            elif body["purge"]:
                doomed = [key for key in self.messages if key[0] == chat_id]
                purged = len(doomed)
                for key in doomed:
                    del self.messages[key]
            return httpx2.Response(200, json={"selected": self.selected, "purged_messages": purged})
        if path == "/search":
            params = request.url.params
            hits = [
                {
                    "account_id": ME,
                    "chat_id": message["chat_id"],
                    "chat_title": self.chats[message["chat_id"]]["title"],
                    "chat_kind": self.chats[message["chat_id"]]["kind"],
                    "message_id": message["message_id"],
                    "sent_at": message["sent_at"],
                    "sender_id": message["sender_id"],
                    "sender_name": message["sender_name"],
                    "text": message["text"],
                    "reply_to_id": message["reply_to_id"],
                    "media": message["media"],
                    "is_outgoing": message["is_outgoing"],
                }
                for message in self.messages.values()
                if params.get("q", "") in message["text"]
                and ("chat_id" not in params or int(params["chat_id"]) == message["chat_id"])
            ]
            return httpx2.Response(200, json={"hits": hits[: int(params.get("limit", 20))]})
        if path == "/chats":
            chats = [
                {
                    "chat_id": cid,
                    "title": chat["title"],
                    "kind": chat["kind"],
                    "messages": len(self.ids(cid)),
                    "oldest_at": WHEN.isoformat(),
                    "newest_at": WHEN.isoformat(),
                    "selected": cid in self.selected,
                }
                for cid, chat in self.chats.items()
            ]
            return httpx2.Response(200, json={"chats": chats})
        return httpx2.Response(404, json={"detail": "Not Found"})
