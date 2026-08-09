"""The bounded crawl loop: caps, concurrency, and the one deadline everything else answers
to.

`crawl_site()` is what turns one `fetch_page()` call (`internals/fetcher.py`) into a run: it
fetches the seed, then a caller-supplied frontier, under six hard caps read from `Settings`
(`app.core.settings` — `crawl_max_pages`, `crawl_max_wall_clock_s`, `crawl_max_bytes`,
`crawl_request_timeout_s`, `crawl_concurrency`, `crawl_politeness_delay_ms`). **Hitting a cap
is a success, not a failure**: the crawl returns whatever pages it collected before the cap
tripped, and `CrawlResult.stats["cap_hit"]` names which one (or `None`, if it finished on its
own). Only the SEED failing to fetch at all is a failure — `CrawlResult.seed_error` is what
`app.features.crawl.service.CrawlService.execute_run` checks to decide that.

**The frontier is a parameter, not something this module discovers.** `extra_urls` is
whatever the caller already knows about beyond the seed — since PER-176, that is
`internals/url_ranking.py`'s `select_urls` output over whatever `internals/sitemap.py`'s
`discover_sitemap_urls` found, wired together one layer up in `service.py`. `extra_urls`
staying a plain parameter is what let the page cap be tested without either pipeline existing
yet, and it is exactly the seam PER-176's discovery-and-selection pipeline plugged into.

**PER-178 made that frontier dynamic, and did it without moving discovery in here.** A site
with no sitemap has nothing for `extra_urls` to carry, and the only place its links exist is
in the seed page's own HTML — which does not exist until this function has already fetched it.
So `crawl_site` grew a second, optional way to be handed a frontier: `frontier_from_seed`, a
caller-supplied function invoked once, on the fetched seed page, when and only when
`extra_urls` is empty. **This module still does not discover anything itself**: it does not
parse a single byte of any page's content to find a link, and it does not know or care that
the function it calls does. Both frontiers land in the same list, under the same truncation,
the same page cap, and the same politeness gate; the difference is only WHEN the caller gets
to compute one. What remains genuinely out of scope here, and is not a thing this seam can
grow into by accident, is recursion: `frontier_from_seed` is called exactly once per run, on
the seed alone, and the pages fetched from the frontier are never handed back to it (see
`crawl_site`'s own parameter docstring, and `app/features/crawl/internals/links.py`).

**Two independent timeout mechanisms, not one.** A monotonic deadline (`clock() +
limits.max_wall_clock_s`) is checked before every non-seed fetch is even attempted — cheap,
and it is what lets the run stop between requests without waiting for one to time out. The
whole crawl additionally runs inside `async with asyncio.timeout(limits.max_wall_clock_s)`,
which is the backstop for the case the first mechanism cannot cover: a single connection that
is already in flight and hangs past its own per-request timeout. Without the second
mechanism, one stuck socket could keep a run alive well past its wall-clock budget; the
per-fetch check alone only ever runs *between* fetches.

**`robots.txt` is obeyed, as of PER-191, and this module holds exactly one corner of it.**
`internals/robots.py`'s `parse_robots` never runs here — that happens once, in
`internals/sitemap.py`, on the ONE `robots.txt` fetch a run makes — but this module is where
the seed's own answer is honoured. `crawl_site` gains `is_allowed`, a synchronous predicate
consulted for the SEED ONLY, before the seed is ever fetched: when it says no, `seed_error`
becomes a `RobotsDisallowedError` and the function returns exactly as it does for any other
seed failure, never opening a socket for a URL its own operator asked this crawler not to
fetch. Frontier URLs are NOT re-checked here — `internals/url_ranking.py`'s `select_urls`
already filtered them upstream, under its own `"robots_disallowed"` rule, and a second check
in this module would either duplicate that counter or silently disagree with it. `crawl_site`
also gains `gate`, an optional caller-supplied `PolitenessGate` (`internals/fetcher.py`) — see
that parameter's own docstring, and `internals/robots.py`'s module docstring for why the gate
handed to THIS function may already be wider than `limits.politeness_delay_ms` by the time it
arrives.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from app.core.settings import Settings
from app.features.crawl.internals.blocked import BlockReason, merge_block_reason
from app.features.crawl.internals.fetcher import (
    ByteBudget,
    ByteBudgetExceededError,
    PolitenessGate,
    fetch_page,
)
from app.features.crawl.internals.ssrf import Resolver
from app.features.crawl.schemas import CrawledPage


logger = logging.getLogger(__name__)


def _origin_of(url: str) -> str:
    """`url`'s scheme and host, lowercased — the comparison `_same_origin_as_seed` makes.

    Deliberately simpler than `internals/url_ranking.py`'s `_origin`, which collapses default
    ports because it compares URLs a sitemap wrote by hand against a seed a user typed. Both
    sides here are a `CrawledPage.url`, which `internals/fetcher.py` built from an already
    normalized, already validated target, so the two spellings this would have to reconcile
    cannot both occur in one run.
    """
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    """The crawl's six hard caps, read once from `Settings` at the top of a run.

    Plain `Settings` fields, not module constants — unlike `app/worker/settings.py`'s
    `POLL_DELAY_SECONDS`/`MAX_JOBS`, which are deliberately kept off `Settings` for
    arq-specific reasons documented there. There is no equivalent reason to hide these.
    """

    max_pages: int
    """The crawl fetches at most this many pages, seed included."""

    max_wall_clock_s: float
    """This run's own wall-clock budget. See the module docstring for why it is enforced
    twice — a pre-fetch check plus an outer `asyncio.timeout` — and
    `Settings.crawl_max_wall_clock_s` for why, inside a deployed worker,
    `WorkerSettings.job_timeout` usually lands first anyway."""

    max_bytes: int
    """The run-wide response-body budget, shared by every page via one `ByteBudget`
    (`internals/fetcher.py`) — **unless the caller passes its own `budget` to `crawl_site`**,
    in which case this field is never consulted at all. `service.py` always does: it builds
    one `ByteBudget(limits.max_bytes)` before sitemap discovery runs and hands the same
    object to both `discover_sitemap_urls` and `crawl_site`, so the run-wide cap this field
    names is enforced either way — just not by reading `max_bytes` a second time here. See
    `crawl_site`'s `budget` parameter for the full reasoning."""

    request_timeout_s: float
    """Not read by this module. Already baked into the shared `httpx.AsyncClient` by
    `build_crawl_client` (`app/features/crawl/http_client.py`) — `fetch_page` passes no
    per-call `timeout=` of its own. Carried here anyway so `CrawlLimits` is the one place
    that names all six caps `Settings` declares, for a caller inspecting one crawl's
    configuration as a whole."""

    concurrency: int
    """How many frontier fetches may be in flight at once, via `asyncio.Semaphore`."""

    politeness_delay_ms: int
    """Minimum gap between the START of one frontier request and the next, across the
    whole run — see `internals/fetcher.py`'s `PolitenessGate`. **Unless the caller passes its
    own `gate` to `crawl_site`, in which case this field is never consulted at all** — the
    same "caller-supplied value wins" shape `max_bytes` documents above. `service.py` always
    passes one: it builds a single `PolitenessGate` before sitemap discovery runs, widens it
    to `internals/robots.py`'s `effective_crawl_delay_ms(...)` once discovery has returned,
    and hands the SAME, already-widened gate to `crawl_site` — so this field can name the
    operator's own configured floor while the gate actually in force may already be wider."""

    @classmethod
    def from_settings(cls, settings: Settings) -> "CrawlLimits":
        """Build the six caps for one run from the process-wide `Settings`."""
        return cls(
            max_pages=settings.crawl_max_pages,
            max_wall_clock_s=settings.crawl_max_wall_clock_s,
            max_bytes=settings.crawl_max_bytes,
            request_timeout_s=settings.crawl_request_timeout_s,
            concurrency=settings.crawl_concurrency,
            politeness_delay_ms=settings.crawl_politeness_delay_ms,
        )


@dataclass(slots=True)
class CrawlResult:
    """What one `crawl_site()` call hands back — always, whether a cap tripped or not."""

    pages: list[CrawledPage]
    """Every page fetched successfully, seed first, then the frontier in whatever order
    concurrent fetches happened to finish. `internals/llms_txt.py`'s `generate_llms_txt`
    sorts before it derives anything from this, precisely because that order is not
    deterministic."""

    stats: dict[str, Any]
    """The exact shape `runs.stats` stores: `pages_crawled`, `pages_failed`,
    `bytes_fetched`, `duration_ms`, `cap_hit`, `pages_empty_content`, `pages_blocked`, and
    `blocked_reason`. `pages_crawled` is the key name the websites feature's reader already
    reads out of this column — see `app.features.websites.internals.websites_reader` — so it
    is spelled exactly that way here, not `len(pages)` renamed to something more natural in
    isolation.

    `pages_empty_content` is counted here, alongside the other five, rather than in
    `internals/run_stats.py` — see that module's own docstring for the line it draws between
    the crawl loop's concerns and persistence-shaped ones. How many of the pages THIS LOOP
    fetched came back with no extractable content is a fact about the fetch, not about how
    the result gets stored, so it belongs on this dict the same way `pages_failed` and
    `bytes_fetched` do.

    `pages_blocked` and `blocked_reason` (added by this feature's WAF-detection ticket) are
    the identical kind of fact, for the identical reason: whether a page's response was a
    detected access challenge or denial is something only THIS loop can know, because it is
    the only code that saw every response as it arrived — `_note_block` below is where both
    are updated. `pages_blocked` counts every fetched page, seed or frontier, whose response
    `internals/blocked.py`'s `classify_block` matched; `blocked_reason` is those pages'
    reasons folded through `merge_block_reason` into one run-level answer, `None` when nothing
    this run fetched was blocked. See `internals/run_stats.py`'s `RUN_STATS_VERSION` 11
    paragraph for how these two reach `runs.stats` unchanged, with no new keyword argument on
    `build_run_stats` — they arrive already present in this dict and pass through its
    `**crawl_stats` spread exactly as `pages_crawled` itself does."""

    cap_hit: str | None
    """`"pages"`, `"bytes"`, `"wall_clock"`, or `None` if the crawl exhausted its frontier
    (or failed on the seed) before any cap bound it. Duplicated onto `stats["cap_hit"]` —
    both exist because callers that only care about the artifact still want this without
    reaching into a dict, and `runs.stats` needs the dict shape regardless."""

    seed_error: Exception | None
    """Set only when the seed itself could not be fetched — the one failure mode that
    makes the whole run a failure rather than a capped success, because there is nothing to
    build an artifact from. `CrawlService.execute_run` maps this to a sanitized message; it
    is never shown to a caller as-is."""

    seed_origin: str | None = None
    """The scheme and host the seed's fetch actually landed on, after every redirect — the
    origin this run is *about*, and the one `app.features.crawl.service` hands
    `generate_llms_txt` as `site_url`.

    **The seed's FINAL origin, not the URL the website was registered with**, and the two
    differ in exactly one interesting case: a site that has moved hosts. Registering
    `example.com` after it began redirecting wholesale to `example.net` should produce an
    artifact about `example.net`, because that is where every page in it came from — titling
    it `example.com` would name a host that appears nowhere in the document. The registered
    URL stays the fallback in `service.py` for the one case this is `None`.

    `None` when the seed never landed — any `seed_error` at all, a blocked or non-2xx seed
    included. There are no pages in that case either, so nothing downstream needs it.
    Defaulted rather than required so the handful of tests that build a `CrawlResult` by hand
    keep working unchanged.
    """


class RobotsDisallowedError(Exception):
    """Raised (as `CrawlResult.seed_error`) when the run's own `is_allowed` predicate refuses
    the SEED URL — a site's `robots.txt` disallows crawling it.

    **The decision this exception encodes is to HONOUR the refusal and fail the run**, rather
    than silently crawling the seed anyway (this crawler has no per-website "ignore
    robots.txt" override — ARCHITECTURE.md §11) or silently producing an empty artifact
    instead. `app.features.crawl.service.CrawlService._safe_error_message` maps this to the
    fixed string "This site's robots.txt disallows crawling this URL." — no new database
    column, because `runs.error` is already free text and already the UI's rendered failure
    reason — and `_is_retryable` treats it as permanent by NOT listing it: a site's
    `robots.txt` will still disallow the same URL in sixty seconds, so retrying it is pure
    latency, the same reasoning that already applies to `SsrfBlockedError` and `FetchError`.

    **Known limitation, stated rather than hidden: the predicate sees the CONFIGURED seed
    URL, not the post-redirect one.** `fetch_page` follows redirects internally and this
    check runs before the seed is ever fetched, so a site that redirects an allowed URL into a
    disallowed path is not caught by this check — only `robots.txt`'s answer for the URL a
    run was actually configured with. Closing that gap would mean fetching, at minimum, the
    redirect chain's headers before this decision could be made, which is a materially
    different (and slower) shape than "consult a synchronous predicate before the first
    request."

    Deliberately its OWN type, following the rule `FetchError` and `ByteBudgetExceededError`
    already set for this feature: an exception lives in the module that raises it.
    """


class AccessBlockedError(Exception):
    """Raised (as `CrawlResult.seed_error`) when the SEED's own response is a detected WAF/CDN
    access challenge or denial — `internals/blocked.py`'s `classify_block` returned a
    `BlockReason` for it.

    **Mirrors `RobotsDisallowedError` above, deliberately, down to the shape of this
    docstring**: the decision this exception encodes is to HONOUR the block and fail the run,
    never to attempt to get past it. This crawler does not solve interactive challenges,
    render JavaScript, spoof or rotate its `User-Agent`, replay cookies, or route around a
    block through a proxy — a WAF or a challenge saying no is a "no" this crawler accepts, the
    same way a `robots.txt` `Disallow` is (CLAUDE.md's explicit scope note for this ticket;
    ARCHITECTURE.md §11 records the same boundary). The user's one escape hatch is social: ask
    the site operator to allowlist `llms-text-bot/0.1`
    (`app.features.crawl.http_client.CRAWL_USER_AGENT`).

    A blocked seed is deliberately treated as harshly as a seed genuinely offline: there is
    nothing to build an artifact from, and a run that "completed" over a Cloudflare challenge
    page's five-kilobyte JavaScript shell — reporting an index of zero pages while calling the
    zero pages it excluded ones with "no extractable content" — is the exact false claim this
    ticket exists to stop making. Retrying changes nothing: a WAF's challenge is deterministic
    for a non-browser client and carries no `Retry-After`, so `_is_retryable`
    (`app.features.crawl.service`) treats this as permanent by NOT listing it, the same
    reasoning `RobotsDisallowedError`, `FetchError`, and `SsrfBlockedError` already get.
    `app.features.crawl.service.CrawlService._safe_error_message` maps `.reason` to one of two
    fixed strings via `_BLOCKED_MESSAGES` — no new database column, `runs.error` already being
    free text.

    **Known limitation, the same one `RobotsDisallowedError` states for itself: this sees the
    response for whatever URL was actually fetched, after `fetch_page`'s own redirect
    handling** — a site that redirects an allowed-looking seed straight into a challenge is
    caught here just as reliably as one that serves the challenge on the configured URL
    directly, because this reads the FINAL response, not the first one.
    """

    def __init__(self, reason: BlockReason, url: str) -> None:
        super().__init__(f"seed blocked ({reason}): {url}")
        self.reason = reason
        """The `BlockReason` `internals/blocked.py`'s `classify_block` returned for the seed's
        response — `"challenge"` or `"denied"`. Read by `_safe_error_message`
        (`app.features.crawl.service`) to pick which of the two fixed, safe-to-display
        strings `runs.error` gets."""


class SeedHttpError(Exception):
    """Raised (as `CrawlResult.seed_error`) when the SEED's final response is not 2xx and
    `internals/blocked.py`'s `classify_block` did NOT recognise it as an access block.

    **This is the complement of `AccessBlockedError`, not an overlap with it.**
    `classify_block` answers one narrow question — "is this a WAF or CDN refusing this
    crawler?" — and deliberately answers `None` for a `404`, a `429`, and an ordinary `5xx`,
    because those are a site answering honestly rather than denying access (see that module's
    numbered rules). Honest or not, none of them is the page that was asked for, and every one
    of them used to reach `generate_llms_txt` as an ordinary `CrawledPage`: a `404` page has a
    real `<title>`, so a run whose seed 404'd published an artifact headed `# 404 Not Found`
    and recorded itself `completed`. The two exceptions together are what close that gap —
    this one owns every non-2xx response `classify_block` passes over.

    **Retry classification lives with the status, and splits.** Unlike every other seed error
    in this module, this one is retryable for SOME values and permanent for others, so
    `_is_retryable` (`app.features.crawl.service`) reads `.status` rather than matching the
    type alone: `5xx` and `429` are a site that may well answer differently in five minutes,
    while a `404` will still be a `404` on the third attempt. That split is exactly the one
    `service.py`'s own retry docstring described as deferred until the crawl pipeline was
    designed.

    **Known limitation, identical to `AccessBlockedError`'s and stated for the same reason:**
    this reads the FINAL response after `fetch_page`'s own redirect handling, so a seed that
    redirects into a soft `404` is caught here exactly as reliably as one that serves it
    directly.
    """

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"seed returned HTTP {status}: {url}")
        self.status = status
        """The final response's HTTP status code. Read by `_safe_error_message` to pick the
        message `runs.error` gets, and by `_is_retryable` to decide whether this attempt
        earns another — both in `app.features.crawl.service`."""


async def crawl_site(
    client: httpx.AsyncClient,
    seed_url: str,
    *,
    limits: CrawlLimits,
    extra_urls: Sequence[str] = (),
    frontier_from_seed: Callable[[CrawledPage], Sequence[str]] | None = None,
    budget: ByteBudget | None = None,
    gate: PolitenessGate | None = None,
    is_allowed: Callable[[str], bool] | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CrawlResult:
    """Crawl `seed_url` plus `extra_urls`, under `limits`, and return whatever resulted.

    Fetches the seed first and alone. If it fails, this returns immediately with
    `seed_error` set and an empty `pages` — there is nothing else to attempt. Otherwise the
    rest of `extra_urls` is fetched with bounded concurrency, a politeness gate on request
    starts, a page cap, and a run-wide byte cap; a non-seed page failing for any reason (an
    `httpx` error, `SsrfBlockedError` on a redirect, a malformed response) is logged at
    WARNING, counted in `stats["pages_failed"]`, and never fails the run.

    Args:
        client: The shared crawl client (`build_crawl_client`). Every fetch goes through it.
        seed_url: The one URL this run is guaranteed to attempt.
        limits: The six caps for this run — see `CrawlLimits`.
        extra_urls: The rest of the frontier, already known to the caller BEFORE the seed is
            fetched — since PER-176, `service.py`'s
            `select_urls(discover_sitemap_urls(...))` pipeline. Truncated to
            `limits.max_pages - 1` up front, and re-checked against the live page count
            before each fetch. Empty is an ordinary value, not a degenerate one: it is what a
            site with no sitemap produces, and it is the condition `frontier_from_seed` below
            answers.
        frontier_from_seed: How to derive a frontier from the SEED PAGE ITSELF, for the
            caller that could not know one up front. Called at most once per run — after the
            seed fetch succeeds, and only when `extra_urls` is empty — with the fetched
            `CrawledPage`, and whatever sequence it returns becomes the frontier, truncated
            and capped exactly as `extra_urls` would have been. `service.py` passes
            `internals/links.py`'s `extract_links` fed through the same `select_urls` the
            sitemap path uses (PER-178); every other caller, and every caller that already
            has a sitemap-derived frontier, leaves it `None`.

            **Three properties of this parameter are the whole depth-1 rule**, and none of
            them is enforced by the function it is given: it is called exactly ONCE, it is
            called with the SEED page only, and its result is fetched but never fed back into
            it. There is no frontier queue, no visited set, and no depth counter anywhere in
            this module, because with one extraction per run there is no second level for any
            of them to bound. `tests/test_crawler_caps.py` pins the "second level is never
            reached" half of that directly, with a callback that would happily return more
            links if it were ever called a second time.

            **Two obligations on whatever is passed here, neither of them enforced by this
            module.** First, it MUST NOT RAISE. It is called inside this function's own
            `asyncio.timeout` block but outside every `except` clause that could absorb it,
            so an exception escaping it would leave `crawl_site` entirely and reach
            `CrawlService.execute_run`'s `except Exception`, which turns anything it catches
            into a FAILED run — a site whose markup could not be read would fail rather than
            producing the single-page run it deserves. `service.py`'s `_select_seed_links`
            discharges this with a `try/except` of its own, exactly as
            `discover_sitemap_urls` does for the sitemap path; it is not defended a second
            time here, because a bare `except` around this call would also swallow real bugs
            in a caller's own code and report them as "this site had no links".

            Second, it is SYNCHRONOUS, and that is load-bearing in two directions. A function
            that cannot `await` cannot make an HTTP request, which is what makes "the fallback
            costs no extra fetch" a property of the type rather than a rule to remember. The
            flip side is that it cannot be interrupted: it runs to completion on the event
            loop, so `limits.max_wall_clock_s` cannot cut a pathological parse short, and the
            time it takes is time no other job in this worker process gets. That is the same
            property `internals/fetcher.py` already has for the `extract_content` call it
            makes per page, and it is bounded the same way — by `crawl_max_bytes` on the body
            being parsed, and by `links.MAX_LINKS` on what comes out.

            Skipped entirely when `extra_urls` is non-empty, so a run cannot end up with a
            frontier assembled from two sources: a sitemap is a site operator's own statement
            about which pages matter, and links scraped off one page do not get a vote
            alongside it. Never called at all when the seed fetch fails — there is no page to
            derive anything from, and the run is already a failure.
        budget: A caller-supplied `ByteBudget` to spend from, instead of a fresh one built
            from `limits.max_bytes`. Both caller-supplied production values, alongside
            `extra_urls` — `resolver`/`clock` below are the test-injection pair instead.
            `service.py` always passes one: it builds a single `ByteBudget(limits.max_bytes)`
            BEFORE calling `crawl_site` at all, so that `internals/sitemap.py`'s
            `discover_sitemap_urls` can spend from it first and this run-wide cap is honored
            across both phases rather than resetting between them. **When a budget is
            injected, `limits.max_bytes` is never consulted** — see `CrawlLimits.max_bytes`'s
            own docstring. `stats["bytes_fetched"]` (== `budget.used`) then includes whatever
            the caller already spent before this function was ever called, which is correct
            — `bytes_fetched` and `cap_hit == "bytes"` must always agree on the same counter
            (`internals/sitemap.py`'s module docstring makes the same argument in more
            detail) — but it does mean `bytes_fetched / pages_crawled` is no longer a page's
            average size once any pre-crawl discovery ran. Defaults to
            `ByteBudget(limits.max_bytes)`, matching every call site before this parameter
            existed.
        gate: A caller-supplied `PolitenessGate` (`internals/fetcher.py`) to serialize
            frontier request starts through, instead of a fresh one built from
            `limits.politeness_delay_ms`. `service.py` always passes one — the SAME gate
            `discover_sitemap_urls` already used for its own fetches, possibly already
            widened past `limits.politeness_delay_ms` by `internals/robots.py`'s
            `effective_crawl_delay_ms` (see `CrawlLimits.politeness_delay_ms`'s own
            docstring). Defaults to `PolitenessGate(limits.politeness_delay_ms / 1000,
            clock)`, matching every call site before this parameter existed.
        is_allowed: A synchronous predicate consulted for the SEED URL ONLY, before it is
            fetched — `None` means "no robots.txt restriction," and is what every call site
            before PER-191 is equivalent to. When it returns `False`, `seed_error` becomes a
            `RobotsDisallowedError` and this function returns immediately, exactly as it does
            for any other seed failure — no request is made. `service.py` passes
            `RobotsRules.is_allowed` (`internals/robots.py`), bound to the rules
            `discover_sitemap_urls` already parsed from the run's one `robots.txt` fetch.

            **Two obligations, the same two `frontier_from_seed` carries and neither enforced
            by this module.** It MUST NOT RAISE — `RobotsRules.is_allowed` never does, by its
            own contract — and it is SYNCHRONOUS, so consulting it can never itself make a
            request. Frontier URLs are NOT re-checked against this predicate: they were
            already filtered upstream by `internals/url_ranking.py`'s `select_urls` under its
            own `"robots_disallowed"` rule, and re-checking them here would either duplicate
            that counter or silently disagree with it (module docstring).

            **Known limitation:** this sees the CONFIGURED `seed_url`, never a post-redirect
            one — `fetch_page` follows redirects internally, after this check has already
            passed. A redirect into a disallowed path is not caught. See
            `RobotsDisallowedError`'s own docstring.
        resolver: Forwarded to every `fetch_page` call. Tests inject a fake one; production
            code never does.
        clock: The monotonic clock the wall-clock deadline and the politeness gate are
            measured against. Defaults to `time.monotonic`; tests inject a fake one to make
            the wall-clock cap deterministic without a real sleep.

    Returns:
        A `CrawlResult`. `asyncio.CancelledError` — arq's own job timeout or a SIGTERM —
        is deliberately allowed to propagate unchanged; catching it here would hide a
        worker shutdown from the reaper that is meant to notice it later.
    """
    budget = budget if budget is not None else ByteBudget(limits.max_bytes)
    pages: list[CrawledPage] = []
    pages_failed = 0
    pages_blocked = 0
    blocked_reason: BlockReason | None = None
    pages_http_error = 0
    pages_off_origin = 0
    seed_origin: str | None = None
    cap_hit: str | None = None
    seed_error: Exception | None = None

    start = clock()
    deadline = start + limits.max_wall_clock_s
    gate = gate if gate is not None else PolitenessGate(limits.politeness_delay_ms / 1000, clock)
    semaphore = asyncio.Semaphore(limits.concurrency)

    def _mark_cap_hit(hit: str) -> None:
        """Set `cap_hit` to `hit` and log it — but only the first caller to arrive.

        Called from every site below that can trip a cap, three of which run inside
        concurrent `fetch_frontier_url` tasks. The `if cap_hit is not None: return` guard at
        the top of that function only stops a task that starts AFTER a cap has already
        fired; it says nothing about two tasks reaching the same cap in the same moment,
        which without this helper would log that cap twice.

        What makes the check-and-set here safe is that nothing between them awaits, so the
        event loop cannot hand control to another task in the middle of it. The pair is
        atomic in practice even though the five call sites are not synchronized with each
        other in any other way: exactly one INFO line per run, whichever of them wins.
        """
        nonlocal cap_hit
        if cap_hit is None:
            cap_hit = hit
            logger.info(
                "crawl: hit the %s cap", hit, extra={"cap_hit": hit, "pages_crawled": len(pages)}
            )

    def _note_block(reason: BlockReason) -> None:
        """Record one page — seed or frontier — whose response `internals/blocked.py`'s
        `classify_block` matched.

        Called from both the SEED branch below (before `seed_error` is set to
        `AccessBlockedError`) and `fetch_frontier_url` (in place of `pages.append`), so a
        run's `pages_blocked`/`blocked_reason` reflect every blocked response this loop saw
        regardless of which of the two ever led to a failed run. `merge_block_reason` is what
        keeps `blocked_reason` deterministic across concurrent frontier fetches whose finish
        order is not reproducible between two runs of the same crawl — see that function's own
        docstring (`internals/blocked.py`).

        Not guarded the way `_mark_cap_hit` explicitly is: two frontier tasks calling this at
        "the same moment" is fine here, unlike a cap, because incrementing a counter and
        folding a `merge_block_reason` call are each safe to run twice in a row with no
        coordination — nothing about EITHER operation depends on which of two concurrent
        callers reaches it first, so there is no result to protect a second caller from
        corrupting.
        """
        nonlocal pages_blocked, blocked_reason
        pages_blocked += 1
        blocked_reason = merge_block_reason(blocked_reason, reason)

    def _note_http_error() -> None:
        """Record one frontier page dropped for a non-2xx status. Unguarded for the same
        reason `_note_block` is: incrementing a counter needs no coordination between two
        concurrent frontier tasks."""
        nonlocal pages_http_error
        pages_http_error += 1

    def _note_off_origin() -> None:
        """Record one frontier page dropped for landing on another host. Unguarded, as
        `_note_http_error` above."""
        nonlocal pages_off_origin
        pages_off_origin += 1

    def _is_ok(page: CrawledPage) -> bool:
        """Whether `page`'s final response was a 2xx — the one status test this loop makes.

        Spelled the same way `internals/sitemap.py` already spells it for the `robots.txt` and
        sitemap fetches it makes, so "a usable response" means one thing across the feature.
        Every non-2xx that reaches here has already been past `classify_block`
        (`internals/fetcher.py` calls it on every response), so anything this returns `False`
        for is by construction a status that module declined to call an access block.
        """
        return 200 <= page.status < 300

    def _redirected_off_origin(requested_url: str, page: CrawledPage) -> bool:
        """Whether fetching `requested_url` ended up on a different host than it asked for.

        **Compares the page against the URL THAT WAS REQUESTED, never against the seed**, and
        that distinction is the whole reason this is safe to do here. `crawl_site` does not
        get to second-guess which URLs its caller put in the frontier — "the frontier is a
        parameter, not something this module discovers" (module docstring), and origin
        filtering of *candidates* is `internals/url_ranking.py`'s `"off_origin"` rule, one
        layer up. What this notices is different and strictly local: a URL that was asked for
        and answered by somebody else. The caller's choice is respected; only the redirect is
        judged.

        That is also exactly the shape of the bug it exists for. Every off-origin page
        observed in a real run arrived this way — a `stripe.com` URL answering from
        `assets.ctfassets.net`, an `anthropic.com` URL answering from `claude.com` — because
        the frontier reaching this loop was already same-origin by construction. A page that
        moved hosts mid-fetch is a page about a different site, and listing it in this site's
        `llms.txt` puts a host in the document that the document is not about.

        The SEED is deliberately not subject to this: a site that has moved wholesale should
        be crawled where it moved to, which is what `seed_origin` records.
        """
        return _origin_of(page.url) != _origin_of(requested_url)

    async def fetch_frontier_url(url: str) -> None:
        nonlocal pages_failed
        async with semaphore:
            if cap_hit is not None:
                return
            if len(pages) >= limits.max_pages:
                _mark_cap_hit("pages")
                return
            if clock() >= deadline:
                _mark_cap_hit("wall_clock")
                return
            await gate.wait_for_turn()
            try:
                page = await fetch_page(client, url, budget=budget, resolver=resolver)
            except ByteBudgetExceededError:
                _mark_cap_hit("bytes")
                return
            except Exception:
                logger.warning("crawl: frontier fetch failed for %s", url, exc_info=True)
                pages_failed += 1
                return
            # Counted, not appended — a blocked frontier page has nothing this run's artifact
            # should list (a Cloudflare challenge shell is not the page it was asked for), but
            # it is not a fetch FAILURE either: the request completed and got an honest answer.
            # Never fails the run — see the module docstring's PER-191 "hitting a cap is a
            # success" pattern, applied here to a WAF blocking a handful of a site's pages
            # rather than to one of the six numeric caps.
            if page.blocked_reason is not None:
                # `CrawledPage.blocked_reason` is typed plain `str | None` (that field's own
                # docstring explains why: `schemas.py` imports nothing from `internals/`), but
                # every value it ever actually holds came from `classify_block`
                # (`internals/blocked.py`) by way of `fetch_page` — this `cast` documents that
                # invariant rather than widening `_note_block`'s own signature to `str`.
                _note_block(cast(BlockReason, page.blocked_reason))
                return
            # Counted and dropped, exactly as a blocked page is, and for the same reason: an
            # honest `404`, `429` or `5xx` is a completed request that did not return the page
            # it asked for, so it is neither a fetch FAILURE nor something this run's artifact
            # should list. Never fails the run — one missing page out of a hundred is an
            # ordinary fact about crawling the internet, and only the SEED's status can end a
            # run (see `SeedHttpError`).
            if not _is_ok(page):
                _note_http_error()
                return
            # Same treatment, one rule later — see `_redirected_off_origin` for why this
            # compares against the requested URL rather than against the seed.
            if _redirected_off_origin(url, page):
                _note_off_origin()
                return
            pages.append(page)

    try:
        async with asyncio.timeout(limits.max_wall_clock_s):
            if is_allowed is not None and not is_allowed(seed_url):
                # Honoured, not merely logged: no request is made at all. See
                # `RobotsDisallowedError`'s own docstring for the decision and its reasoning.
                seed_error = RobotsDisallowedError(f"robots.txt disallows {seed_url}")
            else:
                try:
                    seed_page = await fetch_page(client, seed_url, budget=budget, resolver=resolver)
                except Exception as exc:
                    seed_error = exc
                else:
                    if seed_page.blocked_reason is not None:
                        # Honoured, not defeated — mirrors the `is_allowed` branch above, down
                        # to sitting BEFORE `pages.append`. A blocked seed is never appended:
                        # this run has nothing to build an artifact from,
                        # `frontier_from_seed` is never reached (there is no seed page to
                        # derive a fallback frontier from — this whole block is still the seed
                        # fetch's own `else`), and `generate_llms_txt` is never called at all —
                        # see `AccessBlockedError`'s own docstring for the reasoning. `cast`
                        # for the identical reason the frontier branch above casts — see that
                        # comment.
                        reason = cast(BlockReason, seed_page.blocked_reason)
                        _note_block(reason)
                        seed_error = AccessBlockedError(reason, seed_url)
                    elif not _is_ok(seed_page):
                        # Sits exactly where the block branch above sits, and for the same
                        # reason: BEFORE `pages.append`, so a seed that answered with a `404`
                        # or a `503` never becomes a page an artifact can be built from.
                        # `frontier_from_seed` is never reached either — this is still the
                        # seed fetch's own `else`, so there is no seed page to derive a
                        # fallback frontier from. Counted as well as raised, so a failed run's
                        # partial stats say what happened rather than only `runs.error` does.
                        _note_http_error()
                        seed_error = SeedHttpError(seed_page.status, seed_url)
                    else:
                        # The seed defines the origin every frontier page is measured against
                        # (`_same_origin_as_seed`). Set from the seed's FINAL url, so a site
                        # that moved hosts wholesale is crawled at the host it moved to.
                        seed_origin = _origin_of(seed_page.url)
                        pages.append(seed_page)

                        # The frontier, from whichever of the two sources supplied one. The
                        # `if not frontier` guard is what keeps them mutually exclusive rather
                        # than additive — see `frontier_from_seed`'s own docstring — and it is
                        # also why `frontier_from_seed` cannot be reached at all on the
                        # seed-failure path above: this whole block is the seed fetch's `else`.
                        frontier = list(extra_urls)
                        if not frontier and frontier_from_seed is not None:
                            frontier = list(frontier_from_seed(seed_page))

                        # Truncated up front — but `cap_hit` is deliberately NOT set here yet.
                        # Setting it before the gather below would make every truncated task's
                        # own `if cap_hit is not None: return` check fire immediately, since
                        # that same flag is what marks "stop early" — the truncated frontier
                        # would never run at all. Whether truncation happened is remembered
                        # separately (`frontier_was_truncated`) and only turned into `cap_hit`
                        # once the truncated batch has actually been attempted, below.
                        allowed = max(0, limits.max_pages - 1)
                        frontier_was_truncated = len(frontier) > allowed
                        frontier = frontier[:allowed]

                        if frontier:
                            await asyncio.gather(*(fetch_frontier_url(url) for url in frontier))

                        if frontier_was_truncated and cap_hit is None:
                            _mark_cap_hit("pages")
    except TimeoutError:
        _mark_cap_hit("wall_clock")

    # A run that collected NOTHING is a failed run, not a capped success — and the outer
    # `asyncio.timeout` is the one path that can produce that shape without the seed's own
    # `except` clause having set `seed_error`. It happens when the seed neither succeeds nor
    # fails: a server that accepts the connection and trickles bytes forever keeps each
    # individual socket operation inside the client's per-request timeout, so httpx never
    # raises, and the whole crawl is cut off from the outside instead. Without this, such a
    # run reports `seed_error is None` with an empty `pages`, and `CrawlService` faithfully
    # generates an artifact out of no pages at all and calls it a success.
    #
    # `pages` being empty is exactly equivalent to "the seed never landed": every later
    # append happens after a successful seed fetch, so there is no other way to get here
    # with nothing collected.
    if seed_error is None and not pages:
        seed_error = TimeoutError(
            "the seed URL did not complete within the crawl's wall-clock budget"
        )

    stats = {
        "pages_crawled": len(pages),
        "pages_failed": pages_failed,
        "bytes_fetched": budget.used,
        "duration_ms": int((clock() - start) * 1000),
        "cap_hit": cap_hit,
        # A fact about the pages THIS LOOP fetched, not a persistence-shaped concern —
        # `internals/run_stats.py`'s module docstring draws that line for `links_emitted` and
        # `version`, and how many fetched pages carried no extractable content belongs on this
        # side of it. Counting it here, rather than downstream in `build_run_stats`, also
        # means the seed-failure path in `service.py`
        # (`build_run_stats(result.stats, links_emitted=links_emitted)`) gets this key for
        # free, with no extra plumbing — the persisted shape stays uniform across a
        # successful run's stats and a failed one's partial stats.
        "pages_empty_content": sum(1 for page in pages if page.is_empty),
        # `pages_blocked`/`blocked_reason` (this feature's WAF-detection ticket) are the
        # identical kind of loop-owned fact `pages_empty_content` is, for the identical
        # reason: only this loop saw every response's status and headers as it arrived, seed
        # included — `_note_block` above is what updates both, on the seed branch and inside
        # `fetch_frontier_url` alike. `blocked_reason` is `None` — never an absent key — when
        # nothing this run fetched was blocked, matching every other "0/None is data" field in
        # this dict.
        "pages_blocked": pages_blocked,
        "blocked_reason": blocked_reason,
        # The two exclusions this loop makes that are neither a cap, a failure, nor a block.
        # Recorded for the reason `pages_empty_content` is: the provenance panel draws the
        # gap between `pages_crawled` and `links_emitted` as a set of named reasons, and a
        # reason it cannot name gets silently attributed to whichever one it can — the exact
        # misreport `pages_blocked` was added to stop making.
        "pages_http_error": pages_http_error,
        "pages_off_origin": pages_off_origin,
    }
    return CrawlResult(
        pages=pages,
        stats=stats,
        cap_hit=cap_hit,
        seed_error=seed_error,
        seed_origin=seed_origin,
    )
