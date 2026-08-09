"""The `httpx.AsyncClient` every GitHub request is issued through.

Its own factory rather than a reuse of `features/crawl/http_client.py`'s `build_crawl_client`, and
the reasons are not stylistic — that client is built to fetch *arbitrary, untrusted* sites and
carries three things that are wrong here:

* **An SSRF-blocking transport.** `internals/ssrf.py` exists because a crawl target is a URL a
  user typed, and it must not be allowed to resolve to a private address. `api.github.com` is a
  fixed, public host this codebase chose, so the guard protects against nothing and would add a
  DNS resolution check to every API call.
* **`llms-text-bot/0.1` as the User-Agent.** That string exists so a site operator can identify
  and allowlist our crawler. GitHub wants to identify the *application*, and a request to their
  API claiming to be a crawler is at best confusing in their logs.
* **Crawl-shaped limits.** Redirect and timeout policy tuned for page fetches, not for an API
  that never redirects.

No singleton, matching ARCHITECTURE.md §3.7's rule for the Storage client: a module-level
`AsyncClient` is a connection pool with a lifecycle, and this codebase constructs one per unit of
work instead. The API builds one per request through a FastAPI dependency; the worker builds one
per job.
"""

from typing import Final

import httpx


GITHUB_USER_AGENT: Final = "llms-text/0.1"
"""How this application identifies itself to GitHub.

GitHub requires a User-Agent and rejects requests without one. Deliberately NOT
`CRAWL_USER_AGENT` — that string is a promise to site operators about a crawler's behaviour, and
reusing it here would conflate two audiences that read it for different reasons.
"""

_TIMEOUT_S: Final = 20.0
"""Default timeout for a GitHub request. Every call site in `internals/` also passes its own,
which takes precedence; this is the floor for anything that does not."""

_MAX_CONNECTIONS: Final = 10
"""Connection pool ceiling. Small on purpose: one publication issues at most six sequential
requests, so concurrency here is bounded by how many publications a worker runs at once, not by
anything inside one."""


def build_github_client() -> httpx.AsyncClient:
    """Build a client for GitHub's REST API.

    `follow_redirects` is deliberately **off**. GitHub's API does not redirect for the endpoints
    this feature uses, and following one would forward the `Authorization` header — a live
    installation write token — to whatever host the redirect named. httpx does strip auth across
    hosts on redirect, but relying on that is a worse guarantee than never redirecting at all.
    """
    return httpx.AsyncClient(
        headers={"User-Agent": GITHUB_USER_AGENT},
        timeout=_TIMEOUT_S,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=_MAX_CONNECTIONS),
    )
