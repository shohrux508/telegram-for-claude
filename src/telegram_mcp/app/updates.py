"""Tells the settings page whether a newer release is out on GitHub.

Only reads the public releases list; nothing about the user is sent. Any
failure (offline, rate limit) simply means "no news".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx2

from telegram_mcp import __version__

RELEASES_REPO = "shohrux508/telegram-for-claude"
_API = f"https://api.github.com/repos/{RELEASES_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{RELEASES_REPO}/releases/latest"
_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Update:
    version: str
    url: str


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


async def check(transport: httpx2.AsyncBaseTransport | None = None) -> Update | None:
    """The latest release if it is newer than this program, otherwise None."""
    try:
        async with httpx2.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
            response = await client.get(
                _API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"telegram-for-claude/{__version__}",
                },
            )
        if response.status_code != 200:
            return None
        data = response.json()
        tag = str(data.get("tag_name") or "")
        url = str(data.get("html_url") or RELEASES_PAGE)
    except (httpx2.HTTPError, ValueError, AttributeError):
        return None
    if not is_newer(tag) or not url.startswith(f"https://github.com/{RELEASES_REPO}/"):
        return None
    return Update(version=tag.lstrip("v"), url=url)
