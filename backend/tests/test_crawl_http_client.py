"""Tests for `app.features.crawl.http_client` — the shared crawl client and, specifically,
`_SniKeyedTransport`.

No network: every request is served by an `httpx.MockTransport` this module builds, so what is
asserted is which underlying transport a request was routed to, not what any real server said.

**What this file exists to prevent.** `internals/ssrf.py` dials a validated IP rather than a
hostname, and passes the real hostname as the `sni_hostname` extension. httpcore pools
connections by `(scheme, host, port)` of the URL actually dialed, and an extension is not part
of that key — so before `_SniKeyedTransport`, two hostnames resolving to one IP shared a
keep-alive connection, and the second request's `Host` header went out over a TLS session
negotiated with the first hostname's SNI. A site whose apex redirects to `www` hits that on
every run: `anthropic.com` answered `301`, the follow-up to `www.anthropic.com` reused the
apex's connection, and Cloudflare rejected the mismatch with a `403` that
`internals/blocked.py` correctly reported as a denial — a reachable site failing its run.
"""

import httpx

from app.core.settings import Settings
from app.features.crawl.http_client import (
    CRAWL_BOT_NAME,
    CRAWL_USER_AGENT,
    _SniKeyedTransport,
    build_crawl_client,
)


def _recording_transport(log: list[tuple[int, str]], index: int) -> httpx.MockTransport:
    """A transport that records which instance served a request, and the SNI it carried."""

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((index, str(request.extensions.get("sni_hostname", ""))))
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


async def _get(client: httpx.AsyncClient, *, sni: str) -> httpx.Response:
    """One request shaped exactly as `internals/fetcher.py` shapes them: dialed at an IP,
    with the hostname carried by the extension and the `Host` header."""
    return await client.request(
        "GET",
        "https://192.0.2.10/",
        headers={"Host": sni},
        extensions={"sni_hostname": sni},
        follow_redirects=False,
    )


async def test_two_hostnames_on_one_ip_never_share_a_transport() -> None:
    """The regression, stated directly. Both requests dial the identical URL — the same IP,
    the same port — so httpcore's own pool key cannot tell them apart. Only the SNI can."""
    log: list[tuple[int, str]] = []
    made = 0

    def make() -> httpx.MockTransport:
        nonlocal made
        made += 1
        return _recording_transport(log, made)

    transport = _SniKeyedTransport(make)
    async with httpx.AsyncClient(transport=transport) as client:
        await _get(client, sni="anthropic.com")
        await _get(client, sni="www.anthropic.com")

    assert made == 2, "each hostname must get its own pool"
    assert log == [(1, "anthropic.com"), (2, "www.anthropic.com")]


async def test_the_same_hostname_reuses_one_transport() -> None:
    """Keep-alive within a host is the case that pays — a same-origin crawl of a hundred
    pages must not pay a handshake per page — so the fix must not degrade into
    connection-per-request."""
    log: list[tuple[int, str]] = []
    made = 0

    def make() -> httpx.MockTransport:
        nonlocal made
        made += 1
        return _recording_transport(log, made)

    async with httpx.AsyncClient(transport=_SniKeyedTransport(make)) as client:
        for _ in range(5):
            await _get(client, sni="example.test")

    assert made == 1
    assert [index for index, _ in log] == [1, 1, 1, 1, 1]


async def test_plain_http_requests_share_the_no_sni_pool() -> None:
    """A request with no `sni_hostname` carries no handshake to mismatch, so grouping them
    under one key is correct rather than a gap."""
    log: list[tuple[int, str]] = []
    made = 0

    def make() -> httpx.MockTransport:
        nonlocal made
        made += 1
        return _recording_transport(log, made)

    async with httpx.AsyncClient(transport=_SniKeyedTransport(make)) as client:
        for _ in range(3):
            await client.get("http://192.0.2.10/", headers={"Host": "example.test"})

    assert made == 1


async def test_closing_the_client_closes_every_pool() -> None:
    """One pool per hostname means the close path has to walk them all; a leaked pool is a
    leaked socket for the life of the worker process."""
    closed: list[int] = []

    class _Closable(httpx.MockTransport):
        def __init__(self, index: int) -> None:
            super().__init__(lambda request: httpx.Response(200, text="ok"))
            self._index = index

        async def aclose(self) -> None:
            closed.append(self._index)

    made = 0

    def make() -> httpx.MockTransport:
        nonlocal made
        made += 1
        return _Closable(made)

    transport = _SniKeyedTransport(make)
    async with httpx.AsyncClient(transport=transport) as client:
        await _get(client, sni="a.test")
        await _get(client, sni="b.test")

    assert sorted(closed) == [1, 2]


def test_the_built_client_still_refuses_to_follow_redirects_itself() -> None:
    """`follow_redirects=False` is what keeps `internals/ssrf.py`'s check-then-connect defense
    intact across hops. Swapping the transport must not have disturbed it."""
    settings = Settings()
    client = build_crawl_client(settings)

    assert client.follow_redirects is False
    assert isinstance(client._transport, _SniKeyedTransport)
    assert client.headers["User-Agent"] == CRAWL_USER_AGENT
    assert CRAWL_BOT_NAME == "llms-text-bot"
