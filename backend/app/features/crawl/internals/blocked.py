"""Detecting a WAF/CDN access challenge or an outright denial — never defeating one.

**This module answers exactly one question: does this response look like a machine-issued
"no," rather than the page it was asked for?** It does that from three ordinary, public facts
every response already carries — the status code, the response headers, and (bounded) the
body — and it never does anything with the answer beyond returning it. There is no
challenge-solving here, no headless rendering, no User-Agent spoofing or rotation, no cookie
replay, and no retry loop that tries again hoping for a different answer: a site's WAF saying
no is a "no" this crawler honours, the same way `internals/robots.py` honours a site's own
`robots.txt`. The user's one escape hatch is social, not technical — ask the site operator to
allowlist `llms-text-bot/0.1` (`app.features.crawl.http_client.CRAWL_USER_AGENT`) — and
nothing in this module, or anywhere this module is called from, exists to make that
conversation unnecessary.

**Pure, and never raises** — the same contract `internals/robots.py`'s `parse_robots` and
`internals/links.py`'s `extract_links` hold, and for the same reason: this runs on
attacker-supplied bytes (a hostile or merely broken response body), and a response this
module cannot make sense of is not a reason to fail a run. Every branch below is a plain
comparison; there is no parse step that can throw an exception this function does not catch,
because there is no parse step complex enough to need one.

## The vocabulary, and why it stops at two members

`BlockReason` is `"challenge"` or `"denied"` — deliberately not a richer taxonomy naming
Cloudflare, Akamai, or any other vendor by name. This module classifies BEHAVIOUR (an
interactive challenge a browser could pass and this crawler cannot vs. a flat access denial),
not which product produced it, because the crawler's own response to both is identical: stop,
count it, and say so. A vendor-specific label would invite a vendor-specific workaround later,
which is exactly the "detect and defeat" scope CLAUDE.md's task notes and ARCHITECTURE.md §11
both rule out.

## The five rules, in the order `classify_block` checks them

1. **Any `2xx` status returns `None`, first and unconditionally.** This is what makes a
   regression in this module structurally incapable of touching
   `CrawledPage.is_empty`'s existing, load-bearing meaning (ARCHITECTURE.md §3.4): a
   successful response is never reclassified as a block no matter what its headers or body
   contain, so `is_empty` keeps meaning exactly what it always has — "extraction found
   nothing" — and never becomes a proxy for "this module noticed something odd in a 200."
2. **A `cf-mitigated` response header, present with any value, is a `"challenge"`.**
   Cloudflare sets this header on every response its managed-challenge pipeline intercepts,
   independently of the status code it ultimately returns, so this is the cheapest and most
   direct signal available and is checked before anything that requires reading the body.
   Case-insensitive on the header NAME (HTTP header names are case-insensitive by
   definition — RFC 9110 §5.1) and indifferent to the VALUE, because the header's mere
   presence is Cloudflare's own signal, not any particular string in it.
3. **A `403` or `503` from a `server` header containing "cloudflare", with a small enough
   body and a matching `<title>`, is a `"challenge"`.** Three conditions, all required,
   because any one alone is too weak: the status alone is indistinguishable from an ordinary
   application error; "cloudflare" in `server` alone is true of a huge fraction of the web's
   traffic and says nothing about THIS response; and a `<title>` match alone, with no bound on
   how much of the body was scanned to find it, is a resource-exhaustion path this module will
   not open on attacker-supplied bytes. The body-length bound (`_MAX_BODY_CHARS`) and the
   title-scan window (`_TITLE_SCAN_CHARS`) are independent defensive ceilings in the same
   family `internals/robots.py`'s `MAX_LINES`/`MAX_RULES` and `internals/links.py`'s
   `MAX_LINKS` already are for this feature — bounding the WORK this function will do on a
   hostile body, never merely its own confidence.
4. **A bare `401` or `403` — one that rule 3 did not already classify as a challenge — is a
   `"denied"`.** No challenge markers, just a flat access refusal: HTTP basic auth this
   crawler was never given credentials for, an IP allowlist, or a WAF rule that blocks
   without an interactive step. There is nothing to detect beyond the status code itself.
5. **Everything else, `429` and a bare `503` included, is `None`.** A `429` is a rate limit,
   not an access block — the site is reachable and answering, just asking this crawler to
   slow down, and this module has no way to act on that answer (that is `internals/
   fetcher.py`'s and `internals/crawler.py`'s politeness machinery, not this one's). A bare
   `503` with no Cloudflare markers is an ordinary "temporarily unavailable," identical to
   what an overloaded origin serves on its own with no WAF in front of it at all — reclassifying
   every `503` as a block would turn "the site is briefly down" into "the site refused this
   crawler," which is a different, false claim.

## `merge_block_reason`, and why it has to be order-independent

`internals/crawler.py`'s frontier fetches run concurrently, under `asyncio.gather` — so when
more than one frontier page in the same run is blocked, and for two different reasons, the
ORDER their `fetch_page` calls happen to complete in is not reproducible between two runs of
the identical crawl. A "first observed wins" merge would make `runs.stats["blocked_reason"]`
a function of scheduling jitter rather than of the run itself, which is exactly the kind of
non-determinism `internals/llms_txt.py`'s own docstring goes out of its way to rule out for
the artifact one layer up. `merge_block_reason` is instead a fixed total order —
`"challenge"` outranks `"denied"` outranks `None` — so folding it pairwise over every blocked
page in whatever order they happen to finish always produces the same run-level answer. A
`"challenge"` outranking a `"denied"` is a deliberate choice, not an arbitrary tie-break: an
interactive challenge is the stronger, more specific signal of the two (rule 2 above needs
only ONE header to fire), so a run that saw both reports the one a reader is more likely to
be able to act on.
"""

import re
from collections.abc import Mapping
from typing import Final, Literal


BlockReason = Literal["challenge", "denied"]
"""What kind of access block `classify_block` detected. Deliberately two members — see the
module docstring's "vocabulary" section for why this does not grow a vendor name."""

_CHALLENGE_STATUSES: Final[frozenset[int]] = frozenset({403, 503})
_DENIED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

_MAX_BODY_CHARS: Final = 65_536
"""The body-length ceiling rule 3 requires before it will even attempt a `<title>` scan — see
the module docstring's rule-3 section. A challenge page's own HTML is always small (a few
kilobytes of inline JS and a spinner); a response this large failing every other rule is
evidence it is not one, and scanning it anyway would be unbounded work on a body this module
has no other reason to trust."""

_TITLE_SCAN_CHARS: Final = 4_096
"""How much of the body rule 3's `<title>` scan is allowed to look at, regardless of
`_MAX_BODY_CHARS`'s own, looser ceiling — a `<title>` element sits in `<head>`, at the very
start of any real document, so a match this module cares about is always well inside this
window. Bounding the SCAN separately from the body-length gate is what keeps the regex below
at O(4,096), not O(`_MAX_BODY_CHARS`), on every response that reaches it."""

_CHALLENGE_TITLES: Final[frozenset[str]] = frozenset(
    {"just a moment", "attention required", "checking your browser"}
)
"""The three managed-challenge titles this crawler classifies against, once normalized —
Cloudflare's own interstitials among them (`"Just a moment..."`, `"Attention Required! |
<site>"`). Exact-match against the NORMALIZED title, never a substring: a documentation page
titled "A Moment of Attention: Debugging Race Conditions" is a real page a substring rule
would misclassify, and the title text a challenge vendor ships is not prose a real page would
plausibly reuse verbatim as its own title. See `_normalize_title` for what "normalized"
strips — a trailing ellipsis or exclamation point, and a `" | <site name>"` suffix a
challenge vendor's own template appends — and why stripping exactly those two things, and
nothing else, keeps this an exact match rather than a disguised substring one."""

_TITLE_PATTERN: Final = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_TRAILING_PUNCTUATION: Final = ".…!?"
"""Punctuation `_normalize_title` strips from the END of a title before comparing it —
`"Just a moment..."` and `"Just a moment"` are the same challenge title with two different
amounts of vendor-added punctuation, and this module classifies BEHAVIOUR, not typography."""


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup over a plain `Mapping[str, str]`.

    Not `headers.get(name)`: `httpx.Headers` (what `internals/fetcher.py` actually hands this
    module) is already case-insensitive, but this function is also exercised directly in tests
    against plain `dict`s, and HTTP header names are case-insensitive by definition (RFC 9110
    §5.1) regardless of which `Mapping` implementation happens to be carrying them. Linear in
    the number of headers, which is always small — a handful to a few dozen — so there is no
    reason to reach for anything more than that.
    """
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _normalize_title(raw: str) -> str:
    """`<title>` text, normalized for an exact-match comparison against `_CHALLENGE_TITLES`.

    Two things are stripped, and only two: whatever a challenge vendor's own template adds
    around its interstitial's own title, never anything a real page's title could plausibly
    contain by coincidence. A `" | <site name>"` suffix (`"Attention Required! | example.com"`)
    is a template pattern this crawler's OWN `internals/llms_txt.py` uses too — see that
    module's docstring for `<h1>` selection — so splitting on the first `|` and keeping only
    what precedes it is taking the same "the meaningful part comes before the separator"
    reading a human would. Trailing `_TRAILING_PUNCTUATION` (`"Just a moment..."`) is stripped
    the same way. Lowercased last, so the set membership check itself stays a plain, cheap
    `in` against already-normalized text.
    """
    title = raw.split("|", 1)[0].strip()
    title = title.rstrip(_TRAILING_PUNCTUATION).strip()
    return title.lower()


def _challenge_title(body: str) -> bool:
    """Does `body`'s `<title>` (searched within `_TITLE_SCAN_CHARS`) match one of
    `_CHALLENGE_TITLES` exactly, once normalized by `_normalize_title`?"""
    match = _TITLE_PATTERN.search(body[:_TITLE_SCAN_CHARS])
    if match is None:
        return False
    return _normalize_title(match.group(1)) in _CHALLENGE_TITLES


def classify_block(status: int, headers: Mapping[str, str], body: str) -> BlockReason | None:
    """Classify one fetched response as a `BlockReason`, or `None` if it looks like an
    ordinary response — ARCHITECTURE.md §3.4-style docstring: see the module docstring's
    numbered rules for the full argument behind each branch below; this is the mechanical
    summary.

    **Never raises.** Every comparison here is a plain string/int operation with no I/O, no
    clock, and nothing that can throw on attacker-supplied `headers` or `body` — the same
    "cannot fail a run" contract every other pure module in this feature holds.

    Args:
        status: The final, non-redirect response's HTTP status code — `internals/fetcher.py`
            calls this after redirects have already been followed, so `status` here is never
            a 3xx.
        headers: The final response's headers. Read for `cf-mitigated` (rule 2) and `server`
            (rule 3) only; every other header is ignored.
        body: The final response's decoded body — `CrawledPage.content`, the same string
            `internals/extract.py`'s `extract_content` already parsed. Only ever read for
            rule 3, and even then only its first `_TITLE_SCAN_CHARS` characters.

    Returns:
        `"challenge"`, `"denied"`, or `None` — see the module docstring's numbered rules for
        exactly which responses produce which answer.
    """
    if 200 <= status < 300:
        return None

    if _header(headers, "cf-mitigated") is not None:
        return "challenge"

    if status in _CHALLENGE_STATUSES:
        server = _header(headers, "server")
        if (
            server is not None
            and "cloudflare" in server.lower()
            and len(body) <= _MAX_BODY_CHARS
            and _challenge_title(body)
        ):
            return "challenge"

    if status in _DENIED_STATUSES:
        return "denied"

    return None


_RANK: Final[dict[BlockReason | None, int]] = {None: 0, "denied": 1, "challenge": 2}


def merge_block_reason(
    current: BlockReason | None, incoming: BlockReason | None
) -> BlockReason | None:
    """Fold one more page's `BlockReason` into a run's running total — order-independent, so
    the result does not depend on which of several concurrently-fetched pages happened to
    finish first. See the module docstring's "why order-independent" section for the full
    argument.

    `"challenge"` outranks `"denied"` outranks `None`; ties (both sides equal) return that
    same value unchanged. Called once per fetched page from `internals/crawler.py`'s
    `_note_block`, folded pairwise starting from `None` — the identity element, since
    `merge_block_reason(None, x) == x` for every `x`.
    """
    return current if _RANK[current] >= _RANK[incoming] else incoming
