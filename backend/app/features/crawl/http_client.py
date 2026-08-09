"""The single `httpx.AsyncClient` every crawl fetch is issued through.

Built once per worker process (`app/worker/settings.py`'s `open_worker_resources`, a later
phase of this ticket) and threaded through every `fetch_page` call, the same way the process
shares one asyncpg pool rather than opening a connection per job.

Lives at the top of the feature, not in `internals/`, because `app/worker/settings.py`
constructs it and `internals/` is private to this feature (ARCHITECTURE.md §3.1) — a module
another feature's setup code has to import cannot also be one only this feature may import.
"""

from collections.abc import Callable

import httpx

from app.core.settings import Settings


CRAWL_USER_AGENT = "llms-text-bot/0.1 (+https://github.com/justinhe16/llms-txt-justin-he)"
"""Identifies every crawl request as this project's, with a URL a site operator can visit
to find out what is fetching their pages and how to block it. A descriptive UA is a
courtesy this crawler can afford to extend and cannot afford to skip: it is one grep away
from being the entire explanation a confused site owner gets."""

CRAWL_BOT_NAME = CRAWL_USER_AGENT.split("/", 1)[0].split()[0].lower()
"""The bare product token this crawler answers to — `"llms-text-bot"` — extracted from
`CRAWL_USER_AGENT` above rather than written a second time, so the two cannot drift apart.
The same derivation `internals/robots.py`'s private `_product_token` performs at `robots.txt`
match time; that module needs a FUNCTION rather than this constant, because it has to derive
a token from whatever `user_agent` string a caller (production or a test) hands it, not
always this process's own. This is the plain string for the caller that only ever means THIS
crawler's own token: `app.features.crawl.service`'s `_BLOCKED_MESSAGES` is that caller — the
social escape hatch a detected WAF/CDN block degrades to ("ask the site operator to allowlist
llms-text-bot") needs the crawler to be able to name itself in `runs.error`, and a hardcoded
second copy of the literal is exactly the drift this constant exists to rule out."""


class _SniKeyedTransport(httpx.AsyncBaseTransport):
    """One underlying transport — and therefore one connection pool — per SNI hostname.

    See `build_crawl_client` for the bug this exists to prevent. The rule it enforces is a
    single sentence: **a TLS session negotiated for one hostname is never reused for a request
    carrying a different `Host`.** httpcore cannot enforce that itself here, because the only
    thing it keys on is the dialed URL, and every hostname behind one IP dials the same URL by
    design.

    Keyed on the `sni_hostname` extension rather than on the `Host` header because that
    extension is what actually decides the TLS handshake; `internals/fetcher.py` sets the two
    together (`host` and `host_header` off one `ValidatedTarget`), so they agree, and keying on
    the one that selects the certificate is the one that cannot be fooled by a header a
    redirect rewrote. Plain-`http` requests carry no extension and share the `""` pool, which
    is correct: there is no handshake to mismatch.

    Unbounded by design, and bounded in practice: a run's hostnames are the seed's and
    whatever it redirects to, so this dictionary holds one or two entries. It is not a cache
    and nothing is evicted — `aclose` closes every pool, and the client that owns this
    transport lives exactly as long as the worker process (`app/worker/settings.py`).
    """

    def __init__(self, make_transport: Callable[[], httpx.AsyncHTTPTransport]) -> None:
        self._make_transport = make_transport
        self._by_sni: dict[str, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sni = str(request.extensions.get("sni_hostname", ""))
        transport = self._by_sni.get(sni)
        if transport is None:
            # No lock: this coroutine does not await between the check and the assignment, so
            # the event loop cannot interleave another one into the gap. Two concurrent
            # fetches for the same new hostname therefore cannot both build a pool.
            transport = self._by_sni[sni] = self._make_transport()
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._by_sni.values():
            await transport.aclose()
        self._by_sni.clear()


def build_crawl_client(settings: Settings) -> httpx.AsyncClient:
    """Build the crawler's shared `httpx.AsyncClient`.

    `follow_redirects=False` is LOAD-BEARING, not a style preference. `internals/ssrf.py`'s
    check-then-connect defense only holds if every redirect hop is re-validated by hand
    before the next request is made (`internals/fetcher.py`); if httpx followed redirects
    itself, a validated first hop could still land on an unvalidated final one, silently
    routed through this client with no chance for `validate_url()` to see it. Do not flip
    this to `True` to "simplify" a call site — it removes the one property this whole
    ticket exists to guarantee.

    Constructing this client opens no socket and makes no network call, which is why
    `open_worker_resources` (app/worker/settings.py) can call this unconditionally in
    `on_startup` without a way for it to fail.

    **The transport is `_SniKeyedTransport`, and that is load-bearing too.** httpcore pools
    connections by `(scheme, host, port)` of the URL actually dialed — which, for this client,
    is the validated IP, because `internals/ssrf.py` dials the address it checked rather than
    re-resolving a name. The real hostname travels as the `sni_hostname` extension, and an
    extension is not part of that pool key. Two hostnames on one IP therefore collided on one
    keep-alive connection, and the second request's `Host` header went out over a TLS session
    negotiated with the FIRST hostname's SNI.

    An earlier revision of this docstring recorded that as known, accepted, and unreachable
    "because today a run fetches a single site." **That premise was wrong**, and ordinarily
    so: a site whose apex redirects to `www` — the most common configuration on the web — is
    one site spanning two hostnames, and `internals/fetcher.py` follows that redirect inside a
    single run. `anthropic.com` was the case that surfaced it. The apex answered `301` to
    `www.anthropic.com`, the follow-up reused the apex's connection, and Cloudflare rejected
    the `Host`/SNI mismatch with a `403` that `internals/blocked.py` correctly classified as a
    denial — so a perfectly reachable site failed its run, and its `robots.txt` and sitemap
    probes failed the same way one hop earlier.

    It was never an SSRF hole — both hostnames must pass `validate_url()` independently, and
    no address the guard refused becomes reachable this way — but "not a vulnerability" and
    "not a bug" are different claims, and only the first one held.

    Keep-alive is preserved WITHIN a hostname, which is the case that pays: a same-origin
    crawl of a hundred pages uses one pool, and an apex-to-`www` run uses two.
    """
    timeout = httpx.Timeout(settings.crawl_request_timeout_s)
    limits = httpx.Limits(
        max_connections=settings.crawl_concurrency,
        max_keepalive_connections=settings.crawl_concurrency,
    )
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        transport=_SniKeyedTransport(lambda: httpx.AsyncHTTPTransport(limits=limits)),
        headers={"User-Agent": CRAWL_USER_AGENT},
    )
