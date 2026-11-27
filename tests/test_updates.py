import httpx2
import pytest

from telegram_mcp.app import updates


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("v0.3.0", "0.2.0", True),
        ("0.2.1", "0.2.0", True),
        ("v0.10.0", "0.9.9", True),  # numeric, not alphabetic
        ("v0.2.0", "0.2.0", False),
        ("v0.1.9", "0.2.0", False),
        ("nightly", "0.2.0", False),
        ("", "0.2.0", False),
    ],
)
def test_is_newer(candidate: str, current: str, newer: bool) -> None:
    assert updates.is_newer(candidate, current) is newer


def _github(status: int, body: object) -> httpx2.MockTransport:
    def handle(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == f"/repos/{updates.RELEASES_REPO}/releases/latest"
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(handle)


async def test_newer_release_is_reported() -> None:
    url = f"https://github.com/{updates.RELEASES_REPO}/releases/tag/v99.0.0"
    found = await updates.check(_github(200, {"tag_name": "v99.0.0", "html_url": url}))
    assert found == updates.Update(version="99.0.0", url=url)


async def test_same_version_rate_limit_and_foreign_links_are_ignored() -> None:
    assert await updates.check(_github(200, {"tag_name": "v0.0.1", "html_url": "x"})) is None
    assert await updates.check(_github(403, {"message": "rate limited"})) is None
    phishing = {"tag_name": "v99.0.0", "html_url": "https://evil.example/setup.exe"}
    assert await updates.check(_github(200, phishing)) is None


async def test_network_failure_is_silent() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    assert await updates.check(httpx2.MockTransport(fail)) is None
