"""The artifacts a run publishes: `llms.txt` (a curated index) and `llms-full.txt` (the
corpus behind it). Pure functions — no network, no clock, no I/O, nothing that reads a
setting.

**This module was THE STUB SEAM, then a one-bullet-per-page dump, and is now a curated
index.** ARCHITECTURE.md §3.4 and CLAUDE.md #9 held everything downstream of a fetched page
behind `generate_llms_txt(pages, *, site_url)` while that pipeline was undesigned; PER-179
replaced the stub with a real artifact that listed every survivor, alphabetized, under a
heading derived from its URL's leading path segment. External review of that shape on a real
site (90 links, ~19 KB, the homepage filed under "Other") found it read like a crawl dump
rather than an index a model or a person would want to be handed first. This ticket is the
second redesign of the same seam, not a third stub: it changes what survives selection, how
survivors are grouped and ranked, and what a bullet says, without changing the seam's outer
shape — `pages` and `site_url` in, `str` out, four functions, still no model, still no
network.

* **Deciding which fetched pages belong in the artifact, how they are grouped, ranked, and
  labelled is still this module's job**, and stays in this one file rather than scattering
  into `app.features.crawl.service` or `internals/crawler.py`.
* **Calling a model is still out of scope, and not merely "not yet implemented".** Everything
  below produces a complete, curated artifact with no model involved, on purpose: when
  summarization does land it is a layer *above* this one, and this module stays the fallback
  that runs when that feature's flag is off or its API call fails. A function that only works
  when an external service answers is not a fallback. Nothing here may grow a network call,
  and nothing here may become conditional on one having happened.

## The seam widened once more, to take `signals`

CLAUDE.md #9 and ARCHITECTURE.md §3.4 both required a ticket that redesigns the seam before
widening it a second time (the first widening added `site_url`, to stop the artifact's origin
being outvoted by a redirected page — see `_origin`). This is that ticket. `signals` is a
`Mapping[str, PageSignals] | None`, keyed by `CrawledPage.url` — the same key every other
argument here is already keyed by. It does not breach "no model, no network": every field on
`PageSignals` is metadata a discovery step already collected before any fetch happened
(`internals/sitemap.py`'s `DiscoveredUrl`, `internals/links.py`'s `extract_links`), the
identical argument `url_ranking.py`'s own module docstring makes for why ranking on that
metadata is not "real crawling logic." `signals=None` and a `signals` map with missing keys
are both first-class: every existing hand-built test fixture that calls this seam with no
`signals` argument at all keeps working unchanged, because the seam degrades to ranking on
URL shape and markdown length alone — exactly what it did before this ticket, for a page
whose signal is unknown.

**`PageSignals.lastmod` may only ever be a RELATIVE comparator, never an absolute recency
decay, because a decay needs the current time and this module reads no clock.**
`internals/url_ranking.py`'s `_recency_term` already solves this exact problem — ranging a
`lastmod` as a fraction between the earliest and latest `lastmod` present in the SAME
candidate set, computed once by the caller — and `_recency_fraction` below copies that shape
structurally (not by import: `_recency_term` is `_`-private to that module).

## Seven stages, composed once

`_index_entries` is the seam every one of the four public functions shares, exactly as before
this ticket, and it is now built from seven small, ordered stages rather than one filter-and-
sort. Each stage is a pure function of the previous stage's output — no cache, no shared
mutable state, no early exit one caller takes and another does not (see the "Four public
functions" section below for why sharing one implementation across four callers is
non-negotiable):

1. `_candidates` — filter to on-origin, 2xx, non-empty pages; sort by URL (the determinism
   contract, unchanged since PER-179); label each page; assign it a section; flag whether an
   always-Optional rule already matches it.
2. `_score_all` — rank every candidate. The `lastmod` earliest/latest window is computed ONCE
   here, over this stage's own candidate set, and never recomputed after dedup — recomputing
   it later would make one page's score depend on whether an unrelated page happened to be a
   duplicate.
3. `_dedupe` — collapse pages that are the same content under two URLs, keeping the
   highest-ranked survivor. BEFORE the cap: deduping after would let a duplicate consume a
   main-body slot a distinct page could have had.
4. `_partition` — split into the main body and Optional. An always-Optional match and the
   `MAX_MAIN_BODY_LINKS` cap are both expressed as one rank-ordered walk (see `_partition`'s
   own docstring for why `ALWAYS_OPTIONAL_PENALTY` makes that safe) rather than two separate
   passes. `_ensure_non_empty_main` runs immediately after, as a FLOOR to this ceiling: a run
   whose every candidate matched an always-Optional rule would otherwise leave the main body
   empty — see that function's own docstring for why that reads as broken rather than curated.
5. `_consolidate` — fold a derived section that only survived the cap (and the floor above)
   with one entry into `Other`. Runs AFTER demotion, because demoting entries is what can
   shrink a section to one.
6. `_describe` (with `_boilerplate_descriptions` computed once, over the deduped survivors) —
   turn each surviving page's raw description into a bullet's worth of text, or drop it.
7. `_ordered_entries` — the main body sorted by section then rank, Optional flat by rank,
   concatenated. This is the one list every public function below actually consumes.

`_index_entries(pages, origin, signals)` is what threads all seven (plus the floor) together;
it and `count_indexed_pages`'s `IndexCounts` share a single private `_select` so a count and
the artifact it counts remain structurally incapable of disagreeing (see "Four public
functions" below).

**Deterministic regardless of fetch order**, exactly as before this ticket: every value
derived below is computed from `pages` sorted by URL first, and every ordering decision ends
in a URL tie-break, so a shuffled `pages` list is byte-identical output.
`internals/url_ranking.py`'s `select_urls` makes the same promise for the upstream half.

## Selection is enrichment-invariant

`internals/enrich.py`'s flag-gated, model-assisted pass rewrites a page's `title` and
`description` — never which pages exist, never their bodies — and its own docstring describes
KEEPING PARTIAL RESULTS on its wall-clock timeout as the intended behaviour, not a degraded
case: a run that summarizes 80 of its 100 pages and falls back to extraction for the other 20
is ordinary. So a mix of model-written and extraction-derived labels inside one run is the
common case this module must be correct under, not an edge case. Every stage above that
DECIDES something — which section a page belongs to (`_section_for`), whether it is
always-Optional (`_is_always_optional`), what its dedup identity is (`_fingerprint`), and how
it ranks (`_rank`) — reads only `url`, `origin`, and `markdown`, never `title` or
`description`. Concretely: which pages are indexed, how they are grouped, their main-body/
Optional split, and their order are ALL identical whether `internals/enrich.py` ran, on which
pages it succeeded, or whether it ran at all — `apply_summaries(pages, summaries)` for any
subset `summaries` produces the same selection as the unenriched `pages` it was built from.
Only the label attached to each already-selected page — the H1, the blockquote, and each
bullet's title/description — is free to vary with the flag, because a label is not a selection
decision, and letting the model layer improve labels is the entire point of building it above
this seam rather than inside it. `tests/test_llms_txt.py`'s
`test_selection_is_identical_whether_or_not_enrichment_touched_a_page` pins this directly,
over an ARBITRARY SUBSET of pages enriched — the partial case, not the all-or-nothing one.

## The free-prose block, and never an empty main body

Two more properties this module holds beyond selection and labelling, both aimed at the same
target: every run, on any site, should read like a good hand-written index — a real title, an
orientation, then links — not merely a technically valid document.

**The prose block.** llmstxt.org's own format is H1, blockquote, then ZERO OR MORE markdown
blocks of any kind except a heading, then the `## ` sections — this module used only the first
two elements of that shape until this addition. `_prose_block` supplies up to two: the root
page's own first substantial paragraph, when it exists and is not near-identical to the
blockquote sentence (`_prose_paragraph`; sites routinely lift `og:description` from their own
hero copy, so the two are the same text more often than not), and — only on a run whose index
actually has an `## Optional` section — one fixed sentence naming the main-body/Optional
convention (`_OPTIONAL_ORIENTATION_SENTENCE`). Neither, either, or both may appear; when
neither does, the document goes blockquote straight to the first `## ` exactly as before this
addition.

**The main body is never empty when there is anything to put in it.** `_ensure_non_empty_main`
(stage 4's floor, above) is what guarantees this: a site that is entirely legal pages and
archives still gets a main body naming its best few pages, never H1-blockquote-`## Optional`
with nothing else.

## The shape invariant

For ANY `pages` and ANY `site_url`, both `generate_llms_txt` and `generate_llms_full_txt`
produce a document where all of the following hold — the acceptance criterion this ticket is
actually held to, independent of how well the taxonomy or the ranking weights are tuned for
any one site:

* exactly one H1, and it is the first line;
* exactly one blockquote, immediately after it;
* no heading (a line starting with `#`) inside the free-prose block;
* at least one `## ` section whenever at least one page is indexed;
* no `## ` section with zero bullets under it;
* at most one `## Optional`, and it is the LAST section when present;
* the document ends in exactly one trailing newline.

`tests/test_llms_txt.py`'s `_assert_shape_invariant` is the one assertion helper every test in
this suite that touches `generate_llms_txt`/`generate_llms_full_txt` can call, and a dedicated
matrix of degenerate inputs (zero pages, a root-only run, every page always-Optional, a run
with hundreds of pages, and the rest) asserts it directly — this is deliberately not covered by
a single golden-file test, because the failure mode this guards against is a document that
still parses but READS wrong, on a site the taxonomy or the ranking weights were never tuned
against.

## Four public functions where the ticket named two

`generate_llms_txt` and `generate_llms_full_txt` are the artifacts, and both return `str`
exactly as ARCHITECTURE.md §3.4 pins the seam's shape. `runs.stats` also needs three numbers
only this module can know — how many links the curated index's main body lists, how many
landed under Optional instead, and how many were dropped as duplicates of an already-kept
page — and how many page bodies the full-text expansion had to cut. Widening either
generator's return type to carry them would change the seam's signature; re-deriving them in
`app.features.crawl.service` would put this module's selection rules in a second place, free
to drift from the artifact they claim to describe.

So the counts are their own functions, `count_indexed_pages` (now returning `IndexCounts`,
not a bare `int` — see that dataclass) and `count_full_txt_truncations`, and each shares its
whole implementation with the generator it describes (`_index_entries`/`_select` and
`_plan_full_txt` respectively) rather than reimplementing it. Do not "optimize" this into a
cache or shared mutable state; a pure function that is called twice is the cheap problem here,
and it is what buys the property that a count and the artifact it counts cannot disagree.

A fifth function, `rank_pages`, returns the same selection in the same order as a plain
`list[CrawledPage]` rather than as rendered text or a count — not for `runs.stats`, but so
`app.features.crawl.service` can bias `internals/enrich.py`'s own concurrency-bounded pass
toward the pages that will actually lead the artifact before that pass's own wall-clock
timeout. See `rank_pages`' own docstring for why sharing `_select` a fourth time is what
keeps this from becoming a second, driftable notion of "the pages that matter."
"""

import hashlib
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urlsplit

from app.features.crawl.internals.url_ranking import SITEMAP_DEFAULT_PRIORITY, strip_www
from app.features.crawl.schemas import CrawledPage


MAX_PAGE_TEXT_BYTES: Final = 50 * 1024
"""How much of ONE page's markdown `llms-full.txt` will carry, in UTF-8 bytes (50 KiB —
`50 * 1024`, not 50 000; the ticket's "50 KB" is read the way every other size in this
codebase is).

Bytes, not characters, because the number that matters is how much of a Postgres `text`
column and how much of a worker's memory this consumes, and a character is not a fixed amount
of either — a page of CJK prose averages three bytes per character, so a character budget
would permit roughly three times the column it advertises. Bounded independently of the
crawl's own `crawl_max_bytes` setting on purpose — and NOT read from settings here, which is
what keeps this module pure: that cap bounds what comes off the WIRE, and a 2 MB HTML
document can extract to markdown larger than its own compressed transfer or smaller than a
tenth of it. Neither direction is predictable enough for one cap to stand in for the other."""

MAX_FULL_TEXT_BYTES: Final = 5 * 1024 * 1024
"""How large the whole `llms-full.txt` artifact may get, in UTF-8 bytes (5 MiB).

The per-page cap alone does not bound this — a run may fetch `crawl_max_pages` pages, and
50 KiB times that is not a number this codebase wants to inline into a single column. This is
the cap that actually protects `runs.llms_full_txt`, and it is enforced at a PAGE BOUNDARY
(see `_plan_full_txt`), never mid-page.

A backstop rather than the common path: at today's defaults this binds only if nearly every
page in a run is at its own cap.

**`llms.txt` deliberately gets no equivalent cap**, and the asymmetry is reasoned rather than
an omission. A bullet is a bounded title (`MAX_TEXT_CHARS`), a bounded description (likewise)
and a URL, and — since this ticket — the main body is additionally bounded in COUNT by
`MAX_MAIN_BODY_LINKS`, so the index's size is bounded by (main-body cap + however many pages
Optional carries) times the longest URL in the run, which stays kilobytes for any site a
crawler actually meets. The URL is the one part that is NOT bounded here, and deliberately so:
a truncated URL is a broken link, which is strictly worse than a long one, and dropping
bullets to hit a byte target would defeat the point of an index."""

_RUN_NOTICE_RESERVE_BYTES: Final = 256
"""Bytes held back from `MAX_FULL_TEXT_BYTES` so `_run_truncation_notice` always fits.

Reserved unconditionally, including on runs that never need it, because the alternative is
circular: how many bytes the notice needs depends on how many pages were omitted, which
depends on how much budget was left after the notice. Giving up 256 bytes of 5 MiB buys a
budget that is decided before the first page is appended and a document that is never one
byte over the cap it claims."""

_EMPTY_BODY_NOTICE: Final = "_[No page content was extracted.]_"
"""What a `## {label}` section in `llms-full.txt` carries when `entry.markdown` is empty or
whitespace-only, in place of nothing at all. Only reachable by hand — a real crawl ties
`is_empty` to a non-trivial body length (`internals/extract.py`), so `is_empty=False` with
empty `markdown` never happens outside a test fixture — but the module docstring's shape
invariant holds for ANY input, and a heading with nothing under it reads as a broken document
regardless of how the input that produced it was built. Does not count as truncation
(`_truncate` already returns `was_trimmed=False` for empty input): nothing was cut, so nothing
is reported as cut — this notice exists only to keep the RENDERED section non-empty."""

_PAGE_TRUNCATION_NOTICE: Final = (
    f"\n\n_[Truncated: this page exceeded the {MAX_PAGE_TEXT_BYTES // 1024} KB per-page limit.]_"
)
"""What marks a page whose body hit `MAX_PAGE_TEXT_BYTES`. The artifact says so in its own
text rather than only in `runs.stats`, because a reader — human or model — consuming
`llms-full.txt` on its own has no access to the stats row, and a body that simply stops
mid-sentence is indistinguishable from a page the crawler genuinely found nothing more of."""

MAX_TEXT_CHARS: Final = 500
"""How long a title or a description may be before `_clean` cuts it.

**This is a cap, not a formatting preference, and it is what makes the other two caps hold.**
Every other bound in this module governs a page's markdown, and none of them covers a title
or a description — but both are extracted from a crawled page, which is to say both are
chosen by the site being crawled rather than by this codebase. Without this, a single page
with an eight-megabyte `<title>` produces an eight-megabyte `llms-full.txt` header before the
first page section is even considered, and `MAX_FULL_TEXT_BYTES` never gets a say; the index,
which has no size cap of its own precisely because bullets were assumed to be small, has the
same hole through a long description.

**Still the hard safety bound after this ticket's own, tighter description formatting.**
`_describe` below cuts a description to its first sentence, capped at roughly twenty words —
but that pass is IN ADDITION TO this one, never instead of it: a description with no sentence
boundary at all (one enormous "word") sails through `_describe` unchanged, and `_clean` is
what still catches it. 500 characters is far past any real value and still trivially bounded: a
`<title>` is conventionally under 60, an `og:description` under 200. Cutting at a limit no
honest page reaches means the truncation is invisible in practice and total in the adversarial
case."""

_TEXT_ELLIPSIS: Final = "…"
"""Marks a title or description cut at `MAX_TEXT_CHARS`, so a truncated value is not passed
off as the whole of what the page said."""

_OTHER_SECTION: Final = "Other"
"""The catch-all heading. Three kinds of page land here: those with no leading path segment
at all (the site root when it somehow does not match `origin` — see `_section_for`), those
whose leading segment yields no readable name (a filename like `/sitemap.xml`, or punctuation
that humanizes to nothing, or the reserved name `OPTIONAL_SECTION` itself — see
`_fallback_section`), and — new in this ticket — a DERIVED section that survived selection
with fewer than `_MIN_SECTION_SIZE` entries (`_consolidate`). A canonical taxonomy section
(`_CANONICAL_SECTION_NAMES`) is never folded here, no matter how few entries it has: `Overview`
with one entry is still `Overview`."""

OPTIONAL_SECTION: Final = "Optional"
"""The heading everything demoted out of the main body renders under — public because
`app.features.crawl.service` never needs it directly, but `internals/index_diff.py` does
(`parse_index` sets `IndexEntry.optional` by comparing a parsed section name against this
constant rather than hardcoding the string a second time), and a shared constant is what makes
that comparison provably the same string this module renders, not two spellings that happen to
agree today."""

_ACRONYMS: Final = frozenset(
    {"ai", "api", "cli", "faq", "http", "json", "llm", "sdk", "ui", "ux", "yaml"}
)
"""Path-segment words that are read as acronyms rather than words, so `api-reference` becomes
"API Reference" and not "Api Reference". Consulted per word by `_humanize`, for a DERIVED
section's fallback name — the canonical taxonomy below already spells `API` correctly for any
page it claims, so this only fires for a segment the taxonomy did not match."""

_LABEL_ESCAPES: Final = (("\\", "\\\\"), ("[", "\\["), ("]", "\\]"))
"""Characters that would end a markdown link's `[label]` early, and their escapes. The
backslash is replaced FIRST — order matters, since escaping a bracket introduces a backslash
a later pass would otherwise escape again."""

_TARGET_ESCAPES: Final = ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E"))
"""Characters that would end a markdown link's `(target)` early, percent-encoded rather than
backslash-escaped. Percent-encoding is identity-preserving for a URL — a client following
`%28` arrives exactly where `(` would have taken it — whereas a backslash inside a URL is a
character the URL did not contain. These should be rare: `internals/url_normalize.py` has
already been over every URL that reaches here. Rare is not never, and one unescaped
parenthesis silently corrupts the rest of the line."""


def _empty_document(site_url: str) -> str:
    """What both generators return for `pages == []`: still exactly one H1 and one blockquote,
    so an empty run's artifact is the same SHAPE as a full one rather than a special case every
    consumer has to detect. Never `""` — an empty string in a `text` column is indistinguishable
    from a bug that wrote nothing.

    A function of `site_url`, not a module constant — `site_url` is always available, even on a
    zero-page run, so a reader gets the origin the run was ABOUT rather than the literal string
    `"llms.txt"` naming nothing. Uses `_origin` rather than the raw `site_url`, the same DISPLAY
    form every other H1/blockquote in this module uses.

    A crawl whose seed failed never reaches this module (`CrawlService.execute_run`
    short-circuits on `CrawlResult.seed_error` first), but a frontier that legitimately yielded
    nothing is not this module's problem to raise about."""
    origin = _origin(site_url)
    return f"# {origin}\n\n> No pages were fetched from {origin} during this run.\n"


@dataclass(frozen=True, slots=True)
class PageSignals:
    """Metadata a discovery step already collected about one page, BEFORE it was ever fetched
    — the ranking input this ticket adds. Declared here, next to its one consumer
    (`_score_all`/`_rank`), rather than in `schemas.py`: the same rule `internals/url_ranking.py`'s
    `DiscoveredUrl` follows and states for itself — a shape that crosses no HTTP boundary and
    moves data between two functions in one worker process is a plain frozen dataclass, not a
    Pydantic DTO with an OpenAPI presence neither needs. Public (no leading underscore) because
    `app.features.crawl.service` constructs it.

    Keyed by `CrawledPage.url` — the FINAL, post-redirect URL — in the `signals` mapping every
    public function below accepts. A page absent from that mapping, and a `signals` argument
    of `None` altogether, both mean exactly the same thing: `_NO_SIGNALS`, every field at its
    least-informative value. Ranking on that degraded input is not a special case this module
    handles defensively — it is the ordinary case for any page `service.py` could not match a
    signal to, and every field below is centered or defaulted so that "unknown" and "neutral"
    score identically.
    """

    linked_from_seed: bool
    """Whether the seed page's own `<a href>` markup links to this URL — the run's own
    homepage curating its own site, which is why `SEED_LINK_BOOST` is the single strongest
    term `_rank` computes. `False` for a page the seed does not link to (including the seed
    itself) and for a page this module was given no signal for at all — both are the honest
    "no evidence this page is site-curated" answer, and `_rank` treats them identically."""

    sitemap_priority: float | None
    """The sitemap `<priority>` value the site operator declared, `0.0`-`1.0`, or `None` when
    none was declared (or none was ever discovered for this page at all). `None` and an
    explicit `SITEMAP_DEFAULT_PRIORITY` (`0.5`) score identically in `_rank` — the same
    centering `internals/url_ranking.py`'s own `PRIORITY_WEIGHT` uses, imported from that
    module rather than restated so the two provably agree."""

    lastmod: datetime | None
    """The sitemap `<lastmod>` value, if declared, or `None`. **Relative only, and only among
    the pages in THIS run's own candidate set** — see `_recency_fraction`. Reading this field
    is never, under any composition of calls, a way to learn the current time; nothing in this
    module holds a clock to compare it against."""


_NO_SIGNALS: Final = PageSignals(linked_from_seed=False, sitemap_priority=None, lastmod=None)
"""The signal every page gets when `service.py` found nothing to say about it, and what a
`signals=None` call degrades every page to. One shared instance rather than a fresh one per
lookup, since `PageSignals` is frozen and there is nothing page-specific about "unknown"."""


@dataclass(frozen=True, slots=True)
class IndexCounts:
    """What `count_indexed_pages` returns — `runs.stats["links_emitted"]`,
    `["links_optional"]` and `["links_duplicate"]`, three numbers `_select` computes once and
    shares with the generator that renders the same selection (see the module docstring's
    "Four public functions" section). Declared here for the same reason `PageSignals` is:
    `service.py` is the one place outside this module that constructs or reads one."""

    main: int
    """How many links `generate_llms_txt`'s main body actually lists — the bullets BEFORE
    `## Optional`. What `links_emitted` meant before this ticket (one bullet per surviving
    page) now names only this half of the artifact; see `internals/run_stats.py`'s
    `RUN_STATS_VERSION` 13 paragraph for the full redefinition."""

    optional: int
    """How many links landed under `## Optional` — demoted by an always-Optional rule or by
    the main-body cap, never dropped entirely. `runs.stats["links_optional"]`, new at
    `RUN_STATS_VERSION` 13."""

    duplicate: int
    """How many fetched, on-origin, non-empty, 2xx pages this run's own dedup pass
    (`_dedupe`) collapsed into an already-kept survivor — fetched, real, and STILL not a link
    in either half of the artifact. `runs.stats["links_duplicate"]`, new at `RUN_STATS_VERSION`
    13, and the fourth named reason the provenance panel's invariant needs (see
    `internals/index_diff.py`'s neighbouring concerns and `frontend/lib/crawls/
    run-provenance.ts`): a deduped page is fetched, on-origin, non-empty and a 2xx, so it is
    invisible to every OTHER reason a fetched page can be excluded, and would otherwise be
    uncounted by any of them."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One page that survived stage 1's filter, with everything selection-time needs already
    derived. Distinct from `_Entry` below the same way `internals/url_ranking.py`'s
    `_NormalizedCandidate` is distinct from its public `str` output — a candidate carries the
    facts SELECTION needs (section, always-Optional-ness, depth, a dedup fingerprint); an
    `_Entry` carries the facts RENDERING needs (a formatted description, `optional`). Not part
    of this module's public contract.
    """

    page: CrawledPage
    """The original fetched page. Carried whole, not reduced to its fields, because later
    stages need `page.markdown` (ranking's length term, the dedup fingerprint, `llms-full.txt`'s
    body) and `page.description` (raw, for `_describe` — see that function's own docstring for
    why the RAW value must survive to stage 6 rather than being pre-cleaned here)."""

    url: str
    """`page.url`, carried alongside it so every sort key below reads `candidate.url` rather
    than `candidate.page.url`."""

    label: str
    """What `_label_for(page)` already decided this page is called — computed once, in stage
    1, because every later stage that renders a bullet (rendering, boilerplate comparison)
    needs the identical value. Deliberately NOT read by section assignment or the dedup
    fingerprint — see `_CANONICAL_TAXONOMY` and `_fingerprint`'s own docstrings for why a
    selection decision may never depend on a label enrichment can rewrite."""

    section: str
    """Which heading this page would land under if it stays in the main body — a canonical
    taxonomy name, a derived (humanized-segment) name, or `_OTHER_SECTION`. Provisional until
    `_consolidate` runs: a derived section assigned here can still be folded into `_OTHER_SECTION`
    once the cap has demoted most of its siblings."""

    always_optional: bool
    """Whether `_is_always_optional` already matched this page — legal boilerplate, brand
    assets, taxonomy archives, and the rest of spec §4's stage-1 list. Read by `_rank` (which
    adds `ALWAYS_OPTIONAL_PENALTY`) and by `_partition` (which never lets such a page occupy a
    main-body slot regardless of score)."""

    depth: int
    """`len(_path_segments(url))` — the site root is depth 0. Used by `_rank`'s `DEPTH_BOOST`
    term and by `_dedupe`'s own tie-break (spec §7: "tie-broken by shallower path")."""

    fingerprint: str
    """`_fingerprint(page.markdown)` — the dedup key, deliberately body-only (see that
    function's own docstring for why a label must not enter it). Computed once in stage 1
    rather than recomputed in `_dedupe`, so a candidate's identity for dedup purposes cannot
    drift from the identity every other stage reads it under."""


@dataclass(frozen=True, slots=True)
class _Entry:
    """One page that survived selection, with everything both artifacts need already derived.

    Built once by `_ordered_entries` so the index and the full text cannot disagree about which
    pages are in, what each is called, or what order they come in. `label` and `description`
    are already whitespace-collapsed; escaping happens at render time, not here, because the
    same `label` is a markdown link text in one artifact and a heading in the other, and only
    the first of those needs brackets escaped.
    """

    section: str
    url: str
    label: str
    description: str | None
    markdown: str
    optional: bool
    """Whether this entry renders under `## Optional` rather than in the main body — the
    authoritative flag `generate_llms_txt`'s render loop needs nothing but `section` to render
    correctly (an Optional entry's `section` is already overwritten to `OPTIONAL_SECTION`), but
    `index_diff.py`'s `IndexEntry.optional` and this ticket's `IndexCounts` both read THIS
    field rather than re-deriving it by comparing a string, which is what makes "authoritative"
    true rather than aspirational. Appended last, as this module's stated field-ordering
    convention requires for a field added after the others already existed."""


@dataclass(frozen=True, slots=True)
class _Selection:
    """What `_select` computes once: the final ordered entries, and the one count no public
    caller can derive from them alone. Not part of this module's public contract —
    `count_indexed_pages` reduces this to `IndexCounts`, and `_index_entries` reduces it to
    `entries`, so `generate_llms_txt`/`generate_llms_full_txt` never see a `_Selection` at
    all."""

    entries: list["_Entry"]
    duplicate: int


# --- the canonical taxonomy -----------------------------------------------------------------

_CANONICAL_TAXONOMY: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    (
        "Product",
        frozenset(
            {
                "product",
                "products",
                "platform",
                "features",
                "feature",
                "solutions",
                "solution",
                "pricing",
                "integrations",
                "integration",
                "enterprise",
            }
        ),
    ),
    ("Docs", frozenset({"doc", "docs", "documentation"})),
    (
        "Guides",
        frozenset(
            {
                "guide",
                "guides",
                "tutorial",
                "tutorials",
                "how-to",
                "howto",
                "getting-started",
                "instructions",
            }
        ),
    ),
    ("API", frozenset({"api", "apis"})),
    ("Reference", frozenset({"reference", "references", "spec", "specs", "glossary"})),
    (
        "Research & Data",
        frozenset(
            {
                "research",
                "data",
                "study",
                "studies",
                "report",
                "reports",
                "index",
                "benchmark",
                "benchmarks",
                "insights",
                "state-of",
            }
        ),
    ),
    (
        "Comparisons",
        frozenset(
            {"vs", "versus", "compare", "comparison", "comparisons", "alternative", "alternatives"}
        ),
    ),
    (
        "Customers",
        frozenset(
            {
                "customer",
                "customers",
                "case-study",
                "case-studies",
                "testimonial",
                "testimonials",
                "success",
            }
        ),
    ),
    ("Company", frozenset({"about", "team", "contact", "press", "news", "partners"})),
    ("Blog", frozenset({"blog", "post", "posts", "article", "articles"})),
)
"""The curated sections, checked in this fixed order — the first row whose URL SEGMENTS match
wins. `Overview` is not a row here: it is matched separately, by `_section_for`, on "is this
the origin's root page," checked before this table is ever consulted (spec §3a). Matching is
against ANY path segment — not only the leading one — the same choice
`internals/url_ranking.py`'s `_DOC_SEGMENT_NAMES` makes and documents for itself: a site that
nests its docs under a product name (`/product/docs/setup`) should not be penalized for where
the `docs` segment happens to sit.

**URL segments only — deliberately no title-keyword matching.** An earlier revision of this
table also matched a page's LABEL against a parallel set of title keywords. That is unsound
once a run can enrich only SOME of its pages: `internals/enrich.py`'s `enrich_pages` keeps
partial results on its own wall-clock timeout (its own docstring describes summarizing 80 of
100 pages and falling back to extraction for the other 20 as the intended behaviour, not a
failure), so a run with enrichment on routinely has a MIX of model-written and
extraction-derived titles for structurally identical pages. Reading `title` here would make
two pages that are otherwise the same land in different sections depending on which API call
happened to succeed, and would make a re-run of the identical crawl choose different sections
depending on which pages got summarized that time — both are the determinism contract this
module has held since PER-179, applied to a second kind of nondeterminism this ticket must not
introduce. See `_is_always_optional` and `_fingerprint` for the same rule applied to their own
matching, and the module docstring's "Selection is enrichment-invariant" section for the
contract this preserves.

Indicative, not exhaustive — the table `.claude-surf-spec.md` §3a itself calls "extend as the
fixtures demand," and step 9's real-site crawls are what actually validates or extends it, not
a claim that these are the only segments a real site ever uses."""

_CANONICAL_SECTION_ORDER: Final[tuple[str, ...]] = (
    "Overview",
    *(name for name, _url_segments in _CANONICAL_TAXONOMY),
)
"""`Overview` first — it fixes "the homepage is filed under Other" directly — then the
taxonomy table's own order. `_section_rank` uses this for the main body's H2 order; sections
outside it sort alphabetically after it, and `_OTHER_SECTION` sorts after those (spec §3c)."""

_CANONICAL_SECTION_NAMES: Final = frozenset(_CANONICAL_SECTION_ORDER)
"""Every canonical section name — what `_consolidate` never folds, no matter how few entries
a canonical section has, and what `_section_rank` gives a fixed position to."""


def _is_origin_root_url(url: str, origin: str) -> bool:
    """Whether `url` IS `origin`'s root page — the same scheme/host, an empty (or `/`) path,
    and no query. Shared by `_section_for` (the `Overview` match), `_root_page` (the
    blockquote/H1's source), and nothing else: this is a pure URL-shape test, distinct from
    `_root_page`, which walks `pages` to find the one page satisfying it."""
    parts = urlsplit(url)
    return (
        f"{parts.scheme}://{parts.netloc}" == origin
        and not parts.path.strip("/")
        and not parts.query
    )


def _segment_vocabulary(url: str) -> frozenset[str]:
    """Every whole path segment of `url`, lowercased, PLUS every hyphen-separated word inside
    each segment — the matching surface both the canonical taxonomy and the always-Optional
    rules compare against. `/reports-guides/x` yields `{"reports-guides", "reports",
    "guides"}`, not only the compound whole segment.

    **Still URL-only, and still not title matching** — this widens WHICH PART of the URL is
    read, not WHAT FIELD is read. Found necessary on a real site (step 9 follow-up): a
    URL-segment vocabulary built for single-word segments (`/docs/`, `/blog/`) silently misses
    a site whose own information architecture uses compound, hyphenated slugs
    (`/reports-guides/`, `/vulnerability-reporting/`, `/contact-sales/`) — the exact same
    words this module already looks for, just not alone in their own segment. Splitting every
    segment on `-` and matching sub-words IN ADDITION TO the whole segment is what catches
    both spellings with one vocabulary table rather than two."""
    segments = [segment.lower() for segment in _path_segments(url)]
    words: set[str] = set(segments)
    for segment in segments:
        words.update(segment.split("-"))
    return frozenset(words)


def _section_for(url: str, origin: str) -> str:
    """Which H2 `url` would land under if it survives to the main body.

    `Overview` first and only for `origin`'s own root page (spec §3a) — checked before the
    canonical taxonomy table. Then the taxonomy table, in its fixed order, matched on `url`'s
    OWN path segments and their hyphen-separated sub-words (`_segment_vocabulary`) — no title,
    no label; see `_CANONICAL_TAXONOMY`'s own docstring for why enrichment's partial results
    make that a correctness requirement, not merely a style choice. Then the fallback: the
    leading path segment, humanized, with the same two guards `_section_for` has always
    needed — no usable name humanizes to `_OTHER_SECTION`, and (spec gap 1.5.4) a humanized
    name that collides with `OPTIONAL_SECTION` ITSELF also becomes `_OTHER_SECTION`, so
    `/optional/pricing` cannot silently manufacture a second `## Optional` heading that
    `parse_index` would misattribute.
    """
    if _is_origin_root_url(url, origin):
        return "Overview"
    vocabulary = _segment_vocabulary(url)
    for name, url_segments in _CANONICAL_TAXONOMY:
        if vocabulary & url_segments:
            return name
    return _fallback_section(url)


def _fallback_section(url: str) -> str:
    """The pre-taxonomy behaviour, preserved for a page the canonical table does not claim:
    the leading path segment, humanized. See `_section_for` for the two guards this applies."""
    segments = _path_segments(url)
    if not segments:
        return _OTHER_SECTION
    segment = segments[0].lower()
    if "." in segment:
        return _OTHER_SECTION
    humanized = _humanize(segment)
    if not humanized or humanized == OPTIONAL_SECTION:
        return _OTHER_SECTION
    return humanized


def _section_rank(section: str) -> int:
    """Sort position for a section name in the MAIN BODY: `_CANONICAL_SECTION_ORDER`'s order
    first, then everything else (the caller's next sort key orders those alphabetically), then
    `_OTHER_SECTION` last. Never consulted for `OPTIONAL_SECTION` — Optional entries are never
    interleaved with the main body; see `_ordered_entries`."""
    if section == _OTHER_SECTION:
        return len(_CANONICAL_SECTION_ORDER) + 1
    if section in _CANONICAL_SECTION_ORDER:
        return _CANONICAL_SECTION_ORDER.index(section)
    return len(_CANONICAL_SECTION_ORDER)


def _humanize(segment: str) -> str:
    """A URL slug as a reader would write it: `api-reference` -> "API Reference".

    Split on `-` and `_`, uppercase the words `_ACRONYMS` names, capitalize the rest. Returns
    `""` for a segment with no word characters at all, which `_fallback_section` and
    `_label_for` both treat as "no usable name" rather than emitting an empty heading."""
    words = [word for word in segment.replace("_", "-").split("-") if word]
    return " ".join(word.upper() if word in _ACRONYMS else word.capitalize() for word in words)


def _path_segments(url: str) -> list[str]:
    return [segment for segment in urlsplit(url).path.split("/") if segment]


# --- always-Optional rules (spec §4 stage 1) -------------------------------------------------

_ALWAYS_OPTIONAL_URL_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        # Legal / policy.
        "legal",
        "privacy",
        "privacy-policy",
        "cookie",
        "cookies",
        "cookie-policy",
        "terms",
        "terms-of-service",
        "terms-of-use",
        "tos",
        "dpa",
        "gdpr",
        "subprocessors",
        # Vulnerability disclosure / security.txt. Bare "vulnerability" catches a real site's
        # own `/vulnerability-reporting/` (found on step 9's Profound crawl) via
        # `_segment_vocabulary`'s sub-word split — a compound this list's own
        # "vulnerability-disclosure" entry, matched only as a whole segment, would otherwise
        # miss entirely.
        "vulnerability",
        "vulnerability-disclosure",
        "responsible-disclosure",
        "security.txt",
        ".well-known",
        # Brand / press assets.
        "brand",
        "press-kit",
        "media-kit",
        "assets",
        "logos",
        # Careers.
        "careers",
        "jobs",
        # Archives already dropped at the frontier by `url_ranking.py`'s own pre-filter rules,
        # restated here because this seam is not entitled to assume its input was filtered —
        # see `_index_entries`' own reasoning, extended to this ticket's own stage-1 rule.
        "tag",
        "tags",
        "category",
        "categories",
        "author",
        "authors",
        "changelog",
        "change-log",
        "release-notes",
        "feed",
        "rss",
        "atom",
    }
)

_YEAR_SEGMENT_PATTERN: Final = re.compile(r"^(19|20)\d{2}$")
"""A bare four-digit path segment, 1990-2099 — the same shape
`internals/url_ranking.py`'s private `_is_dated_archive` matches (restated here, not
imported, for the reason every rule in `_ALWAYS_OPTIONAL_URL_SEGMENTS` is restated rather than
assumed already filtered). A dated blog/news/events archive segment (`/events/2023/kickoff`,
`/blog/2022/03/post`) is what this catches — "past events" among spec §4's stage-1 list is a
dated one by definition, so no separate `events` segment rule is needed alongside it."""


def _has_dated_segment(segments: list[str]) -> bool:
    return any(_YEAR_SEGMENT_PATTERN.match(segment) for segment in segments)


def _is_always_optional(url: str) -> bool:
    """Whether `url` already matches one of spec §4's stage-1 rules — legal boilerplate, brand
    assets, taxonomy archives, dated archives, and the rest — and should therefore never occupy
    a main-body slot regardless of how it would otherwise rank. See `_rank`'s
    `ALWAYS_OPTIONAL_PENALTY` for how this flag actually forces the outcome, and `_partition`
    for why that penalty is provably enough.

    **URL segments only — no label, no title.** An earlier revision of this function also
    matched a page's LABEL against a set of title phrases ("privacy policy", "terms of
    service"). Dropped for the identical reason `_CANONICAL_TAXONOMY` dropped its own
    title-keyword column: `internals/enrich.py` can rewrite a label on some pages of a run and
    not others (partial results on its own wall-clock timeout), and whether a page is demoted
    to Optional must not depend on which pages a model call happened to reach. Every rule below
    is either operator-authored URL shape (a `/privacy` path segment) or a legal/brand/archive
    vocabulary a site's own information architecture already encodes in its URLs — which is
    also, incidentally, the more robust signal: a page's own author cannot accidentally give a
    legal page a title that reads like Overview.

    Matched against `_segment_vocabulary(url)` — whole segments AND their hyphen-separated
    sub-words — for the identical reason `_section_for` reads the same vocabulary: a real
    site's own `/vulnerability-reporting/` is exactly the `vulnerability-disclosure` case this
    list already names, spelled with a hyphen this list's exact-segment form alone would
    never match.
    """
    if _segment_vocabulary(url) & _ALWAYS_OPTIONAL_URL_SEGMENTS:
        return True
    return _has_dated_segment([segment.lower() for segment in _path_segments(url)])


# --- ranking (spec §5) ------------------------------------------------------------------------

DEPTH_BOOST: Final = 4.0
"""Worth up to four points at the root, applied as `DEPTH_BOOST / (1 + depth)` — a reciprocal,
not a linear penalty, because the job here is picking thirty links out of however many
survived, not ranking a multi-thousand-URL frontier the way `url_ranking.py`'s linear
`DEPTH_PENALTY` does. A linear term would make depth 8-vs-9 matter as much as 1-vs-2; the
reciprocal saturates instead — past depth ~4 every candidate is within 0.4 of every other on
this term alone, which is exactly where depth should stop being the deciding factor. Swings
`4.0` (root) down to `0.4` (depth 9)."""

SEED_LINK_BOOST: Final = 8.0
"""The strongest term this module computes, and deliberately so: it is the only one that
reflects a human's judgement (the site operator's own homepage) rather than a URL's shape. A
swing of exactly `0` or `8.0` — `PageSignals.linked_from_seed` is a bool, not a magnitude — and
large enough that a depth-3 page in a DERIVED section the seed links to can still outrank a
depth-1 page in a canonical section it does not."""

SECTION_WEIGHTS: Final[dict[str, float]] = {
    "Overview": 6.0,
    "Research & Data": 5.0,
    "Comparisons": 4.5,
    "Product": 4.0,
    "Docs": 4.0,
    "Guides": 3.5,
    "API": 3.0,
    "Reference": 3.0,
    "Customers": 2.0,
    "Company": 1.0,
    "Blog": 0.5,
}
"""A dict, not a tuple index, so a section's weight and its rendering order (`_section_rank`)
are separately readable and can disagree — `Blog` sorts derived-alphabetically in the main
body but scores lowest of every canonical section here, which is the whole point: a citation-
worthy data study should outrank a routine blog post even though "Blog" happens to come later
alphabetically than "Research & Data" would if both sorted as plain sections. The spec requires
only that `Research & Data`, `Comparisons`, `Product`, and `Docs` outrank `Blog`; the exact
numbers between them are tuning, validated against real sites in step 9, not individually
defended. A derived section or `_OTHER_SECTION` scores `0.0` — present in neither list."""

LENGTH_BOOST: Final = 2.0
_LENGTH_BUCKETS: Final[tuple[int, ...]] = (500, 2_000, 8_000)
"""Bucketed on `len(page.markdown)`, not log-scaled: `LENGTH_BOOST * buckets_met /
len(_LENGTH_BUCKETS)` is always an exact multiple of `2.0 / 3`, with no float-drift surface,
and "which of three sizes is this page" is the whole of what this signal is meant to carry — a
mild, fully SATURATING boost (a 900 KB page and an 8 KB page score identically) so a
substantial study outranks a stub without letting one enormous page dominate every other
term."""

PRIORITY_WEIGHT: Final = 2.0
"""The same value `internals/url_ranking.py`'s own `PRIORITY_WEIGHT` uses, and the same
centering: `priority - SITEMAP_DEFAULT_PRIORITY` swings `±1.0`, so an explicit `0.5` and a
`None` (`PageSignals.sitemap_priority`) score identically — `SITEMAP_DEFAULT_PRIORITY` is
imported from that module rather than restated so the two provably use the same center."""

LASTMOD_WEIGHT: Final = 0.4
"""The same value and shape `internals/url_ranking.py`'s own `LASTMOD_WEIGHT` uses: a swing of
only `±0.2`, the smallest weight here, because recency is a tie-breaker among otherwise similar
candidates, never something that should let a freshly-touched but otherwise thin page outrank a
stable, substantial one. See `_recency_fraction`."""

ALWAYS_OPTIONAL_PENALTY: Final = -1000.0
"""A sentinel, not a tuned weight. The maximum positive sum every OTHER term above can
possibly contribute is `SEED_LINK_BOOST (8.0) + DEPTH_BOOST (4.0, at the root) +
max(SECTION_WEIGHTS.values()) (6.0) + LENGTH_BOOST (2.0) + PRIORITY_WEIGHT/2 (1.0, the swing's
positive half) + LASTMOD_WEIGHT/2 (0.2) == 21.2`, so `-1000.0` is two orders of magnitude
clear of it. That arithmetic is what makes "an always-Optional match forces the entry to the
bottom" a PROPERTY of `_rank`'s output rather than a hope about how the other terms usually
behave."""


def _to_utc(value: datetime) -> datetime:
    """Normalize `value` to a timezone-aware UTC `datetime` — the same shape
    `internals/url_ranking.py`'s own `_to_utc` gives, copied structurally (that function is
    `_`-private and cannot be imported) rather than reimplemented from scratch, for the
    identical reason: comparing a naive and an aware `datetime` raises `TypeError`, and a
    stored `lastmod` may legitimately be either depending on whether its sitemap declared a UTC
    offset. A naive value is assumed to already be UTC rather than rejected."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _recency_fraction(
    lastmod: datetime | None, earliest: datetime | None, latest: datetime | None
) -> float:
    """The `LASTMOD_WEIGHT`-scaled contribution of `lastmod` to a candidate's score, ranged
    `-0.5..0.5` before the weight is applied. Modelled directly on
    `internals/url_ranking.py`'s `_recency_term` — copy the STRUCTURE, not the symbol, since
    that function is `_`-private to its own module.

    `earliest`/`latest` are the min and max `lastmod` already present across THIS RUN'S OWN
    candidate set, computed once by the caller (`_score_all`) and never recomputed — recency is
    relative to what this crawl itself discovered, never to the current time, which is the
    whole reason this function reads no clock in any form. `0.0` — the neutral midpoint,
    exactly what a candidate with no `lastmod` at all also receives — when every survivor
    shares one `lastmod` value (`earliest == latest`, nothing to rank within) or when any of
    the three inputs is `None`.
    """
    if lastmod is None or earliest is None or latest is None or earliest >= latest:
        return 0.0
    fraction = (_to_utc(lastmod) - earliest) / (latest - earliest)
    return fraction - 0.5


def _length_term(markdown_length: int) -> float:
    """`buckets_met / len(_LENGTH_BUCKETS)` — see `_LENGTH_BUCKETS` for why this is bucketed
    rather than log-scaled."""
    met = sum(1 for bucket in _LENGTH_BUCKETS if markdown_length >= bucket)
    return met / len(_LENGTH_BUCKETS)


def _rank(
    candidate: _Candidate,
    signal: PageSignals,
    earliest_lastmod: datetime | None,
    latest_lastmod: datetime | None,
) -> float:
    """One candidate's score — every term documented at its own constant above, summed, with
    `ALWAYS_OPTIONAL_PENALTY` added last when `candidate.always_optional` is set. Every sort
    that uses this score ends in `url` as its final tie-break (`_dedupe`, `_partition` via its
    caller, `_ordered_entries`), the same mechanism `internals/url_ranking.py`'s `select_urls`
    uses for its own `(-score, url)` sort, so a shuffled `pages` input still produces
    byte-identical output."""
    priority_term = (
        0.0
        if signal.sitemap_priority is None
        else signal.sitemap_priority - SITEMAP_DEFAULT_PRIORITY
    )
    score = (
        DEPTH_BOOST / (1 + candidate.depth)
        + (SEED_LINK_BOOST if signal.linked_from_seed else 0.0)
        + SECTION_WEIGHTS.get(candidate.section, 0.0)
        + LENGTH_BOOST * _length_term(len(candidate.page.markdown))
        + PRIORITY_WEIGHT * priority_term
        + LASTMOD_WEIGHT * _recency_fraction(signal.lastmod, earliest_lastmod, latest_lastmod)
    )
    if candidate.always_optional:
        score += ALWAYS_OPTIONAL_PENALTY
    return score


# --- dedup (spec §7) ---------------------------------------------------------------------------

_FINGERPRINT_BODY_CHARS: Final = 2_000


def _fingerprint(markdown: str) -> str:
    """`sha256` of the first `_FINGERPRINT_BODY_CHARS` of `markdown` — the identity two
    entries are compared on for dedup. `hashlib` is fine in a pure module —
    `internals/index_diff.py` already uses it for the identical reason (a content fingerprint,
    never a security boundary; see that module's `CONTENT_HASH_CHARS`).

    **Markdown only — no label.** An earlier revision fingerprinted `(cleaned label lowercased,
    body hash)`, which is an outright bug once enrichment can label only some pages of a run:
    two duplicate pages where one received a model-written title and the other kept its
    extracted one would fingerprint DIFFERENTLY despite having identical bodies, and dedup
    would silently stop catching them — precisely on the runs where enrichment is enabled,
    which is exactly backwards for a feature meant to improve the artifact. The body is the one
    fact about a page enrichment never touches (`internals/enrich.py`'s `apply_summaries`
    rewrites `title`/`description` only), so it is the only fact this fingerprint may read."""
    return hashlib.sha256(markdown[:_FINGERPRINT_BODY_CHARS].encode("utf-8")).hexdigest()


# --- descriptions (spec §6) --------------------------------------------------------------------

_DESCRIPTION_MAX_WORDS: Final = 30
"""The hard cap `_whole_sentence` gates a description's first sentence against. Not a cut
point — a description's own og:description-shaped sentence is usually well under 20 words,
and a first sentence in the 20-30 range is still emitted WHOLE rather than trimmed to 20; only
past 30 does this module give up and drop the description entirely. See `_whole_sentence`'s
own docstring for why "emit whole or drop" replaced "cut to the nearest word.\""""

_SITE_SENTENCE_MAX_WORDS: Final = 30
_BOILERPLATE_MIN_PAGES: Final = 3
"""Two pages sharing a description is a two-page site telling the truth; three is a site-wide
default `og:description`. Compared on the whitespace- and case-normalized RAW description
(`_normalize_for_boilerplate`) over the DEDUPED survivors (`_boilerplate_descriptions`'s own
docstring) — detectable only here, because a per-page pass could never see every page at
once."""

_SENTENCE_END_PATTERN: Final = re.compile(r"[.!?](?:\s|$)|[。！？]")
"""An ASCII terminator (`.`, `!`, `?`) followed by whitespace or the end of the string — the
trailing-whitespace requirement is what stops "e.g." or "3.14" from reading as a sentence
boundary — OR a CJK terminator (`。`, `！`, `？`), matched unconditionally: CJK prose does not
put a space after sentence-ending punctuation, so requiring one would never match a real CJK
sentence at all. Found necessary on a real site (step 9 follow-up): a description in Japanese,
Chinese, or Traditional Chinese ending in `。` was going undetected as "has a terminator" and
falling through to the whole-text branch — safe for a single-sentence description (the common
case, and the one step 9's four sites happened to produce), but silently wrong the day a
multi-sentence CJK description needs the same first-sentence trim an English one already
gets."""


def _first_sentence(text: str) -> str:
    """`text` up to and including its first sentence-ending punctuation, or the whole of
    `text` when none is found — the same "whatever there is" fallback a title already gets."""
    match = _SENTENCE_END_PATTERN.search(text)
    return text[: match.end()].strip() if match else text


def _whole_sentence(text: str, max_words: int) -> str | None:
    """`text`'s first sentence (`_first_sentence`), emitted WHOLE if it fits within
    `max_words`, or `None` if it does not — never a word-boundary cut partway through it.

    **Replaces a word-boundary `_cap_words` cut that used to ship a real defect**: cutting a
    genuine sentence at the Nth word produces a description ending in a dangling word with no
    terminal punctuation — "…and stay competitive in the" — which is indistinguishable from
    the SITE's own truncation the ellipsis check in `_describe` already exists to catch, and
    is a worse reading experience than the truncated-with-`…` originals this module's whole
    redesign was meant to fix. "Whole sentence or nothing" is the rule for every text field in
    this module drawn from a site's own prose — `_describe`, `_site_sentence`, and
    `_prose_paragraph` (via `_whole_paragraph_or_sentence`) all build on this function or its
    three-tier sibling rather than trimming independently.
    """
    sentence = _first_sentence(text)
    if len(sentence.split()) <= max_words:
        return sentence
    return None


def _normalize_for_boilerplate(text: str | None) -> str:
    return " ".join((text or "").split()).lower()


_PUNCTUATION_PATTERN: Final = re.compile(r"[^\w\s]")


def _normalize_for_comparison(text: str) -> str:
    """Case- and punctuation-insensitive normalization for the "does this description merely
    restate the label" check — `_restates_label`'s only caller."""
    stripped = _PUNCTUATION_PATTERN.sub("", text.lower())
    return " ".join(stripped.split())


def _restates_label(description: str, label: str) -> bool:
    return _normalize_for_comparison(description) == _normalize_for_comparison(label)


def _boilerplate_descriptions(candidates: list[_Candidate]) -> frozenset[str]:
    """Every normalized raw description that appears on `_BOILERPLATE_MIN_PAGES` or more of
    `candidates` — a site-wide default `og:description` this seam can detect precisely because
    it sees every surviving page at once, which a per-page formatting pass never could.

    Args:
        candidates: The DEDUPED survivors (main and Optional together) — computing this over
            the pre-dedup set would let a duplicate page manufacture a false boilerplate hit
            for a description that, among genuinely distinct pages, is not actually shared by
            three of them.
    """
    counts = Counter(
        _normalize_for_boilerplate(candidate.page.description)
        for candidate in candidates
        if candidate.page.description and candidate.page.description.strip()
    )
    return frozenset(
        normalized for normalized, count in counts.items() if count >= _BOILERPLATE_MIN_PAGES
    )


def _describe(raw: str | None, label: str, boilerplate: frozenset[str]) -> str | None:
    """A page's raw `description` turned into a bullet's worth of text, or `None`.

    In order: `None`/whitespace-only stays `None`. A RAW description already ending in `…` or
    `...` is dropped entirely — that ellipsis is the SITE's own truncation, so the text is a
    fragment, and a bare link beats a broken sentence (spec gap 1.5.3: this check MUST run on
    `raw`, before any cut of this module's own, because `_clean` below appends its own `…` when
    it trims at `MAX_TEXT_CHARS` — checking a `_clean`-passed value here would mistake this
    module's own truncation marker for the site's). A description matching `boilerplate` is
    dropped next, on the same raw, normalized text `_boilerplate_descriptions` compared it
    against. What survives is emitted as its first sentence, WHOLE, when that sentence fits
    within `_DESCRIPTION_MAX_WORDS` (`_whole_sentence`) — **never cut at a word boundary
    partway through it**: an earlier revision of this function trimmed to a fixed word count,
    which produces a description ending in a dangling word with no terminal punctuation
    ("…and stay competitive in the"), indistinguishable from the truncated-with-`…` originals
    this whole redesign exists to fix and arguably worse, since the reader gets no signal at
    all that anything was cut. A first sentence that overruns the cap is dropped ENTIRELY
    rather than shortened — a bare link is the clean outcome the llmstxt.org format is built
    to support, and a mid-clause fragment is not. What survives `_whole_sentence` still passes
    through `_clean` — the hard `MAX_TEXT_CHARS` safety net, IN ADDITION TO this rule rather
    than instead of it (see that constant's own docstring) — before a description that merely
    restates `label` is dropped as the final check: a bare link already says as much.
    """
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.endswith("…") or stripped.endswith("..."):
        return None
    if _normalize_for_boilerplate(stripped) in boilerplate:
        return None
    sentence = _whole_sentence(stripped, _DESCRIPTION_MAX_WORDS)
    if sentence is None:
        return None
    cleaned = _clean(sentence)
    if cleaned is None:
        return None
    if _restates_label(cleaned, label):
        return None
    return cleaned


# --- the blockquote (spec §2) -------------------------------------------------------------------


def _root_page(pages: list[CrawledPage], origin: str) -> CrawledPage | None:
    """The one page in `pages` that IS `origin`'s root — consulted even when it is `is_empty`,
    because a JavaScript shell still carries a real `<title>` and a real meta description.
    `None` when no fetched page is the root.

    Shared by `_project_name` (the H1) and `_site_sentence` (the blockquote), which is the
    whole reason this exists as its own function: two call sites independently deriving "the
    root page" is precisely the bug `_ordering_key` was already written to prevent for
    `_project_name` alone (an earlier revision of this module keyed that scan on URL alone, and
    two root pages with different titles produced a different H1 depending on which the caller
    happened to list first). Sharing one `_root_page` result between the H1 and the blockquote
    is what makes them provably describe the same page rather than two independently-resolved
    ones that usually, but not provably, agree.

    Deliberately NOT `PageSignals`-aware and not the same page `PageSignals` is keyed against
    for `linked_from_seed` — see the module docstring's "seed page and root page" distinction:
    the root is `origin`'s own root URL, wherever it fetched from; the seed is whatever URL the
    run was configured with, wherever IT fetched from. The two differ exactly when the seed
    redirects to a non-root path (`/` -> `/en/`).
    """
    for page in sorted(pages, key=_ordering_key):
        if _is_origin_root_url(page.url, origin):
            return page
    return None


_ATX_HEADING_PATTERN: Final = re.compile(r"^#{1,6}(\s|$)")
"""CommonMark's ATX heading syntax — one to six `#` characters, then whitespace or end of
line — matched at the START of a block only, by `_first_paragraph`. Deliberately narrower
than "starts with `#`": marketing copy genuinely beginning "#1 platform for..." or
"#trending" is not a heading and must not be skipped as one; `# ` and `## Section` are."""


def _paragraph_candidates(markdown: str) -> Iterator[str]:
    """Every non-empty, whitespace-collapsed, NON-HEADING block of `markdown`, split on blank
    lines, in document order — `_first_paragraph`'s and `_prose_paragraph`'s shared candidate
    pool.

    **Skips a block that is itself a markdown heading**, found on real sites during step 9's
    verification: `internals/extract.py`'s markdown sometimes keeps a page's own `# Title¶`
    (a permalink-anchored H1) as its first block. Returning that verbatim would put a line
    starting with `#` inside the free-prose block — the module docstring's own shape
    invariant forbids exactly that, and a raw `# Heading[¶](url)` reads as noise in a
    blockquote or prose paragraph either way, pilcrow included."""
    for block in markdown.split("\n\n"):
        collapsed = " ".join(block.split())
        if not collapsed or _ATX_HEADING_PATTERN.match(collapsed):
            continue
        yield collapsed


def _first_paragraph(markdown: str) -> str | None:
    """The first candidate from `_paragraph_candidates`, or `None` for markdown with none at
    all (empty, all-whitespace, or nothing but headings) — `_site_sentence`'s second-choice
    source.

    Deliberately UNQUALIFIED, unlike `_prose_paragraph` below: `_site_sentence` only reaches
    this when the root page has no `description` at all, and even a short, tagline-shaped
    block ("Used by the best marketers in the world") is still a more honest blockquote than
    falling all the way through to the generic page-count sentence one level further down its
    own fallback chain. `_prose_paragraph`'s free-prose block has no such fallback beneath it
    — omitting it entirely costs nothing — so it can afford to be pickier; see
    `_qualifies_as_prose`.
    """
    return next(_paragraph_candidates(markdown), None)


def _site_sentence(root_page: CrawledPage | None) -> str | None:
    """What the blockquote says about the SITE, rather than about the artifact: the root
    page's own `description`, first sentence, emitted WHOLE if it fits within
    `_SITE_SENTENCE_MAX_WORDS` (`_whole_sentence`) — or, when the root page has none usable,
    its markdown's first paragraph (`_first_paragraph`), same treatment — or `None` when
    neither exists (no root page fetched, both fields empty after cleaning, or both sentences
    overran the cap). `None` is a normal, common result, not a failure: `_index_summary`/
    `_full_summary` fall back to a count sentence when this is `None`, and every value
    returned here has already been through `_clean`'s `MAX_TEXT_CHARS` safety bound.

    Never a word-boundary cut partway through the sentence — see `_whole_sentence`'s own
    docstring for the failure mode this avoids: a blockquote reading "…and stay competitive
    in the" is a worse outcome than falling through to the next source, or to the count
    sentence.
    """
    if root_page is None:
        return None
    if root_page.description:
        sentence = _whole_sentence(root_page.description.strip(), _SITE_SENTENCE_MAX_WORDS)
        cleaned = _clean(sentence) if sentence is not None else None
        if cleaned:
            return cleaned
    paragraph = _first_paragraph(root_page.markdown)
    if paragraph:
        sentence = _whole_sentence(paragraph, _SITE_SENTENCE_MAX_WORDS)
        cleaned = _clean(sentence) if sentence is not None else None
        if cleaned:
            return cleaned
    return None


_PROSE_PARAGRAPH_MAX_WORDS: Final = 60
"""Looser than `_SITE_SENTENCE_MAX_WORDS` (30) — the blockquote is a one-line summary; the
prose block is where a reader who wants slightly more before hitting the link sections gets
it. Still capped, and still through `_clean`'s `MAX_TEXT_CHARS` safety net (see
`_prose_paragraph`), for the identical adversarial-input reason every other text field in this
module is bounded."""

_PROSE_MIN_WORDS: Final = 8
"""The minimum length, in words, a markdown block must clear — IN ADDITION TO containing at
least one sentence terminator (`_qualifies_as_prose`) — before `_prose_paragraph` will use
it. A two- or three-word tagline is not "a substantial paragraph" even on the rare occasion
it happens to end in a period; this is what stops a short, punctuated fragment from
qualifying just because the terminator check alone let it through."""

_OPTIONAL_ORIENTATION_SENTENCE: Final = (
    "Pages above are grouped by topic and cover what is most useful for understanding this "
    "site; less central pages — legal, brand, and archival material — are grouped under "
    "Optional below."
)
"""Emitted only when this run's index actually has an `## Optional` section — a sentence about
how THIS DOCUMENT is organized, not about the generator or the crawl (contrast the exclusion
clause PER-179's blockquote used to carry, which this ticket removed for exactly that
confusion). Fixed and count-free on purpose: it explains a convention, not a measurement, so
it reads the same on every run that has an Optional section rather than needing its own
description-formatting pass."""


def _is_near_identical(a: str, b: str) -> bool:
    """Whether `a` and `b` are the same sentence in substance — an exact match after
    `_normalize_for_comparison`, or one being a normalized PREFIX of the other. The prefix case
    is the common one this exists for: a site's `og:description` is routinely lifted verbatim
    from its own homepage's opening copy, so `_site_sentence` (capped at 30 words) is often
    exactly the first 30 words of `_prose_paragraph` (capped at 60) when both are drawn from
    the same underlying text. Not a fuzzy-similarity check — a paraphrase in different words is
    not caught, which is an accepted, deliberately narrow scope: the prose block's whole
    purpose is failing closed (never emitting near-duplicate prose) rather than failing open
    (occasionally missing a paraphrase)."""
    norm_a = _normalize_for_comparison(a)
    norm_b = _normalize_for_comparison(b)
    if not norm_a or not norm_b:
        return False
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    return longer.startswith(shorter)


def _qualifies_as_prose(text: str) -> bool:
    """Whether `text` reads as a real, sentence-shaped paragraph rather than hero-strip
    chrome — at least one sentence terminator (`_SENTENCE_END_PATTERN`) AND at least
    `_PROSE_MIN_WORDS` words.

    Found on a real site (step 9 follow-up): a homepage's first markdown block is routinely a
    tagline — "Used by the best marketers in the world" — real text, but not a paragraph, and
    not what a reader wants standing in for orientation prose. The terminator check alone
    catches that example; the length check on top of it is what stops a short-but-punctuated
    fragment ("Sign up today.") from qualifying just because it happens to end in a period.
    """
    if not _SENTENCE_END_PATTERN.search(text):
        return False
    return len(text.split()) >= _PROSE_MIN_WORDS


def _whole_paragraph_or_sentence(text: str, max_words: int) -> str | None:
    """`text` WHOLE if it fits within `max_words`; failing that, its own first sentence
    (`_first_sentence`) if THAT fits; `None` if even the first sentence overruns the cap.

    The three-tier sibling of `_whole_sentence` above, for a candidate that is allowed to be
    more than one sentence long — the free-prose block, unlike a description, is not
    contractually a single sentence. Every tier still emits a COMPLETE unit or nothing: never
    a word-boundary cut, which could previously land as easily inside a markdown construct
    (`…so you can [buy` — a truncated link, missing its own `](url)`) as it could mid-clause.
    """
    if len(text.split()) <= max_words:
        return text
    sentence = _first_sentence(text)
    if len(sentence.split()) <= max_words:
        return sentence
    return None


def _prose_paragraph(root_page: CrawledPage | None, site_sentence: str | None) -> str | None:
    """The root page's first QUALIFYING markdown paragraph — real, sentence-shaped prose
    (`_qualifies_as_prose`), never hero-strip chrome — emitted whole up to
    `_PROSE_PARAGRAPH_MAX_WORDS`, or as its own first sentence if the whole paragraph overruns
    that (`_whole_paragraph_or_sentence`; never a word-boundary fragment of either). `None`
    when there is no root page, no candidate block qualifies at all, or every qualifying
    candidate is near-identical to `site_sentence` (`_is_near_identical`) — the blockquote
    already said it, and repeating it one paragraph later is noise rather than orientation.

    Walks EVERY non-heading block in document order (`_paragraph_candidates`), not only the
    first: a homepage's first block is routinely a tagline rather than real prose, and moving
    on to the next block is what finds the actual opening paragraph instead of settling for
    chrome, or for a word-boundary cut through it, or for omitting the section outright when a
    perfectly good paragraph was one block further down.
    """
    if root_page is None:
        return None
    for candidate in _paragraph_candidates(root_page.markdown):
        if not _qualifies_as_prose(candidate):
            continue
        text = _whole_paragraph_or_sentence(candidate, _PROSE_PARAGRAPH_MAX_WORDS)
        if text is None:
            continue
        cleaned = _clean(text)
        if not cleaned:
            continue
        if site_sentence is not None and _is_near_identical(cleaned, site_sentence):
            continue
        return cleaned
    return None


def _prose_block(
    root_page: CrawledPage | None, site_sentence: str | None, *, has_optional: bool
) -> list[str]:
    """Zero or more markdown blocks between the blockquote and the first `## ` section —
    llmstxt.org's own optional element, never a heading (see the module docstring's "Shape
    invariant" section). At most two blocks today: the root page's own prose
    (`_prose_paragraph`), then — only when this run's index actually has an `## Optional`
    section — one fixed orientation sentence explaining the main-body/Optional convention
    (`_OPTIONAL_ORIENTATION_SENTENCE`). Either, both, or neither may be present; when neither
    is, the document goes blockquote straight to the first `## ` exactly as it did before this
    addition.
    """
    blocks: list[str] = []
    paragraph = _prose_paragraph(root_page, site_sentence)
    if paragraph is not None:
        blocks.append(paragraph)
    if has_optional:
        blocks.append(_OPTIONAL_ORIENTATION_SENTENCE)
    return blocks


def _index_summary(site_sentence: str | None, total: int, origin: str) -> str:
    """`llms.txt`'s blockquote: what the site is, in its own words, when this run found any —
    never a claim about the generator ("an index of N pages...") unless it had to fall back.
    The exclusion clause this blockquote used to carry ("Excludes N pages with no extractable
    content") is gone entirely as of this ticket — that number already lives in
    `runs.stats["pages_empty_content"]`, and a reader opening the artifact wants to know what
    the SITE is, not how the crawler's selection rules behaved."""
    if site_sentence is not None:
        return f"> {site_sentence}"
    return f"> An index of {_page_count(total)} from {origin}."


def _full_summary(site_sentence: str | None, total: int, origin: str) -> str:
    """`llms-full.txt`'s blockquote: the same site sentence, followed by a short clause naming
    what this specific document is, so the stored column stays self-describing even when read
    apart from `llms.txt`."""
    if site_sentence is not None:
        return f"> {site_sentence} The full text of {_page_count(total)} from {origin}."
    return f"> The full text of {_page_count(total)} from {origin}."


def _page_count(count: int) -> str:
    return "1 page" if count == 1 else f"{count} pages"


# --- rendering (unchanged since PER-179 — see the module docstring's stage list) --------------


def generate_llms_txt(
    pages: list[CrawledPage],
    *,
    site_url: str,
    signals: Mapping[str, PageSignals] | None = None,
) -> str:
    """Build the `llms.txt` index for `pages`, in the llmstxt.org shape, curated rather than a
    one-bullet-per-page dump.

    Exactly one H1 (the project name), exactly one blockquote (a sentence about the SITE, not
    the generator — see `_site_sentence`), then an H2 per curated section holding at most
    `MAX_MAIN_BODY_LINKS` links in rank order, and — when anything was demoted — one final
    `## Optional` holding the rest, flat, in rank order:

    ```
    # Acme Docs

    > Acme helps engineering teams ship documentation their users actually read.

    ## Docs

    - [Configuration](https://acme.test/docs/config): How to configure Acme.

    ## Optional

    - [Privacy Policy](https://acme.test/privacy)
    ```

    Selection, ranking, deduping, the cap, and description formatting are all `_index_entries`'
    job (via `_select` — see the module docstring's seven-stage list); this function's own
    job is unchanged since PER-179: render the ordered entries it is handed, with no structural
    change to how sections are emitted.

    Pages whose extraction came back empty (`CrawledPage.is_empty`), whose response was not a
    2xx, or whose final URL is not on `site_url`'s origin never reach selection at all — see
    `_candidates`' own docstring for why all three are restated here rather than assumed
    already filtered.

    Args:
        pages: Every page `internals/crawler.py`'s `crawl_site` collected for one run, in
            whatever order they finished fetching. Never sorted by the caller — sorting
            happens here, once, so every caller gets determinism for free.
        site_url: The site this artifact is ABOUT — any URL on it; only its scheme and host
            are read. Supplied by the caller rather than derived from `pages`, because a
            derived origin can be outvoted by one redirected page (see `_origin`). The
            service passes `CrawlResult.seed_origin`, falling back to the registered website
            URL.
        signals: Ranking metadata keyed by `CrawledPage.url`, or `None` — see `PageSignals`.
            `None` and a mapping missing some pages' entries both degrade gracefully to
            `_NO_SIGNALS` for the pages not covered.

    Returns:
        The artifact, ending in a newline. `_empty_document(site_url)` for `pages == []`. See
        the module docstring's "Shape invariant" section for what holds true of every return
        value regardless of input.
    """
    if not pages:
        return _empty_document(site_url)

    origin = _origin(site_url)
    sig = signals if signals is not None else {}
    entries = _index_entries(pages, origin, sig)
    root_page = _root_page(pages, origin)
    site_sentence = _site_sentence(root_page)
    has_optional = any(entry.optional for entry in entries)
    lines = [
        f"# {_project_name(root_page, origin)}",
        "",
        _index_summary(site_sentence, len(entries), origin),
    ]
    for block in _prose_block(root_page, site_sentence, has_optional=has_optional):
        lines.extend(("", block))

    section: str | None = None
    for entry in entries:
        if entry.section != section:
            section = entry.section
            lines.extend(("", f"## {section}", ""))
        lines.append(_bullet(entry))

    lines.append("")
    return "\n".join(lines)


def generate_llms_full_txt(
    pages: list[CrawledPage],
    *,
    site_url: str,
    signals: Mapping[str, PageSignals] | None = None,
) -> str:
    """Build the `llms-full.txt` corpus for `pages`: the same H1 and a blockquote of its own,
    then `## {title}` and that page's markdown, once per page, **in the same order the index
    lists them** — main body first (section order, then rank within a section), then Optional,
    flat, in rank order.

    Carries EVERYTHING the index does, main body and Optional alike (spec §8): a page demoted
    to Optional for exceeding the main-body cap or for matching an always-Optional rule has not
    lost its content, only its prominence, and the expansion is where that content still lives
    in full.

    The two artifacts are read together: a consumer that finds a link in `llms.txt` and wants
    the page behind it should find the bodies in the order the links were in.

    **No `<|firecrawl-page-N-lllmstxt|>` separators**, deliberately — see the historical note
    this module has carried since PER-179: Firecrawl's implementation emits them and then
    strips them out again with a regex before anything consumes the result.

    Both caps live in `_plan_full_txt` — see that function's own docstring for how they are
    enforced, and `count_full_txt_truncations` for the number a run records about it.

    Args:
        pages: As `generate_llms_txt`.
        site_url: As `generate_llms_txt`.
        signals: As `generate_llms_txt`.

    Returns:
        The artifact. `_empty_document(site_url)` for `pages == []`. Never larger than
        `MAX_FULL_TEXT_BYTES` when encoded as UTF-8, and always ending in EXACTLY one trailing
        newline (the module docstring's shape invariant) — a page whose own markdown is empty
        (hand-constructible only; see `_truncate`) would otherwise leave its own `## {label}`
        section's template trailing blank, compounding into more than one newline at the very
        end of the document when that page is last. `rstrip` normalizes only the OUTER edge of
        the joined string, never anything mid-document.
    """
    sig = signals if signals is not None else {}
    joined = "".join(_plan_full_txt(pages, site_url, sig)[0])
    return joined.rstrip("\n") + "\n"


def count_indexed_pages(
    pages: list[CrawledPage],
    *,
    site_url: str,
    signals: Mapping[str, PageSignals] | None = None,
) -> IndexCounts:
    """How many links `generate_llms_txt(pages)` lists, split by where — `runs.stats
    ["links_emitted"]` (now `IndexCounts.main`), `["links_optional"]`, and
    `["links_duplicate"]` (`internals/run_stats.py`, `RUN_STATS_VERSION` 13).

    Shares `_select` with the generator rather than recounting, so this can disagree with the
    artifact only if the artifact disagrees with itself — see the module docstring's "Four
    public functions" section. Deliberately not computed by the caller from `len(pages)`
    minus some subtraction: that is correct only while today's exclusion rules are the ONLY
    ones, which is a property of this module's current selection logic rather than of the
    data. Ask the artifact what it listed.
    """
    origin = _origin(site_url)
    sig = signals if signals is not None else {}
    selection = _select(pages, origin, sig)
    main = sum(1 for entry in selection.entries if not entry.optional)
    optional = len(selection.entries) - main
    return IndexCounts(main=main, optional=optional, duplicate=selection.duplicate)


def count_full_txt_truncations(
    pages: list[CrawledPage],
    *,
    site_url: str,
    signals: Mapping[str, PageSignals] | None = None,
) -> int:
    """How many pages `generate_llms_full_txt(pages)` could not carry in full —
    `runs.stats["full_txt_truncated"]`.

    **Counts both ways a page can lose content**, and the distinction is worth stating,
    because "truncated" could be read as only the first:

    * a page whose markdown exceeded `MAX_PAGE_TEXT_BYTES` and was trimmed, and
    * a page omitted entirely once `MAX_FULL_TEXT_BYTES` stopped the append.

    A page that is both trimmed and then omitted counts once: this is a count of pages missing
    content, not of truncation events. It is `0` on every run whose full text is complete.
    """
    sig = signals if signals is not None else {}
    return _plan_full_txt(pages, site_url, sig)[1]


def rank_pages(
    pages: list[CrawledPage],
    *,
    site_url: str,
    signals: Mapping[str, PageSignals] | None = None,
) -> list[CrawledPage]:
    """The pages `_index_entries` would keep, in ARTIFACT ORDER — main body first, then
    Optional, each in the same rank order `_ordered_entries` produces. A fifth public
    function, sharing every stage of the same selection pipeline (`_select`) the other four
    do, so this ordering cannot silently drift from what the artifact actually lists.

    **The one caller this exists for:** `app.features.crawl.service.CrawlService.execute_run`
    passes this — instead of `result.pages` in whatever order `crawl_site`'s racing fetches
    happened to collect it — to `internals/enrich.py`'s `enrich_pages`. That module bounds
    concurrency with a semaphore acquired roughly in LIST order and can time out before every
    page is sent, so a ranked list biases which pages finish enrichment before the deadline
    toward the pages that will actually lead the artifact, rather than toward whichever pages
    happened to fetch first. Selection itself is unaffected either way — see the module
    docstring's "Selection is enrichment-invariant" section — this only changes which pages a
    partial enrichment pass reaches first.

    Because ranking reads only `url`, `origin`, `markdown`, and `signals` — never `title` or
    `description` (the same enrichment-invariance rule every selection stage holds) — this
    function's own output does not depend on whether the pages it is given have already been
    enriched, which is what makes it safe to compute BEFORE `enrich_pages` ever runs.

    A page `_select` drops entirely (empty, non-2xx, off-origin, or a dedup loser) is simply
    ABSENT from the result — `enrich_pages` already only sends a request for a page with
    non-empty, post-truncation markdown, so a page that would never reach the artifact is not
    worth spending a request on either. Dropping it here costs nothing downstream:
    `apply_summaries` (`internals/enrich.py`) iterates the CALLER'S OWN `pages` list — still
    `result.pages`, never this function's output — and looks up each URL in whatever
    `summaries` dict came back, so a page absent from this ranking is simply never enriched,
    never "missing" from anything.
    """
    origin = _origin(site_url)
    sig = signals if signals is not None else {}
    entries = _index_entries(pages, origin, sig)
    by_url = {page.url: page for page in pages}
    return [by_url[entry.url] for entry in entries if entry.url in by_url]


def _plan_full_txt(
    pages: list[CrawledPage], site_url: str, signals: Mapping[str, PageSignals]
) -> tuple[list[str], int]:
    """Decide the whole of `llms-full.txt`: its chunks in order, and how many pages were cut.

    One implementation, two public callers (`generate_llms_full_txt` joins the chunks,
    `count_full_txt_truncations` takes the number), because the caps are arithmetic that must
    not be performed twice from two places.

    **`MAX_FULL_TEXT_BYTES` stops at a page boundary, never mid-page.** Once the next page's
    section would not fit, this stops appending entirely and charges every remaining page to
    the truncation count. Cutting a final page mid-sentence to fill the budget exactly would
    buy a few kilobytes of a document nobody needs to be exactly 5 MiB, at the cost of leaving
    a reader unable to tell a trimmed page from a complete one.

    **First-fit-stop, not best-fit**: this does not skip an oversized page to squeeze in a
    smaller one after it. The output stays a strict PREFIX of the index order.

    The budget covers the whole rendered document, headings and blank lines included, because
    what is being bounded is the size of a Postgres value rather than the size of the prose.
    """
    if not pages:
        return [_empty_document(site_url)], 0

    origin = _origin(site_url)
    entries = _index_entries(pages, origin, signals)
    root_page = _root_page(pages, origin)
    site_sentence = _site_sentence(root_page)
    has_optional = any(entry.optional for entry in entries)
    header_lines = [
        f"# {_project_name(root_page, origin)}",
        "",
        _full_summary(site_sentence, len(entries), origin),
    ]
    for block in _prose_block(root_page, site_sentence, has_optional=has_optional):
        header_lines.extend(("", block))
    header = "\n".join(header_lines) + "\n"

    chunks = [header]
    budget = MAX_FULL_TEXT_BYTES - _RUN_NOTICE_RESERVE_BYTES
    used = len(header.encode())
    truncated = 0

    for position, entry in enumerate(entries):
        body, was_trimmed = _truncate(entry.markdown)
        if not body.strip():
            body = _EMPTY_BODY_NOTICE
        section = f"\n## {entry.label}\n\n{body}\n"
        size = len(section.encode())
        if used + size > budget:
            omitted = len(entries) - position
            chunks.append(_run_truncation_notice(omitted))
            return chunks, truncated + omitted
        chunks.append(section)
        used += size
        truncated += int(was_trimmed)

    return chunks, truncated


def _run_truncation_notice(omitted: int) -> str:
    """The line that closes a `llms-full.txt` stopped by `MAX_FULL_TEXT_BYTES`, naming how
    many pages are missing. Symmetric with `_PAGE_TRUNCATION_NOTICE`, and for the same
    reason: a document that simply ends is indistinguishable from a complete one. Sized to
    fit `_RUN_NOTICE_RESERVE_BYTES` at any page count this crawler can reach."""
    limit = MAX_FULL_TEXT_BYTES // (1024 * 1024)
    return f"\n_[Truncated: {_page_count(omitted)} omitted at the {limit} MB per-run limit.]_\n"


def _truncate(markdown: str) -> tuple[str, bool]:
    """Trim `markdown` to `MAX_PAGE_TEXT_BYTES` if it is over, and say whether it was.

    Sliced on the ENCODED bytes and decoded with `errors="ignore"`, so a cut landing inside a
    multi-byte character drops that character rather than emitting a lone continuation byte or
    a replacement character.
    """
    encoded = markdown.encode()
    if len(encoded) <= MAX_PAGE_TEXT_BYTES:
        return markdown, False
    kept = encoded[:MAX_PAGE_TEXT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}{_PAGE_TRUNCATION_NOTICE}", True


# --- the seven-stage selection pipeline (spec Part 1.2) ----------------------------------------

MAX_MAIN_BODY_LINKS: Final = 30
"""The main body's hard cap — inside the spec's 25-35 target. Demoting to Optional, not
dropping: everything past this cap is still in the artifact, still in `llms-full.txt`, just
not in the curated top of `llms.txt`. See `_partition`."""

_MIN_SECTION_SIZE: Final = 2
"""A DERIVED section (never a canonical one — see `_CANONICAL_SECTION_NAMES`) with fewer
entries than this, after the cap has run, folds into `_OTHER_SECTION`. What kills a section
like "Profound Black Friday Index" surviving as an H2 with exactly one bullet under it."""


def _index_entries(
    pages: list[CrawledPage], origin: str, signals: Mapping[str, PageSignals]
) -> list["_Entry"]:
    """The ordered entries both artifacts are built from — `_select(pages, origin,
    signals).entries`. See the module docstring's seven-stage list for what composes this, and
    `_select`'s own docstring for why the duplicate count travels separately rather than being
    dropped on the floor here."""
    return _select(pages, origin, signals).entries


def _select(
    pages: list[CrawledPage], origin: str, signals: Mapping[str, PageSignals]
) -> _Selection:
    """Run the module docstring's seven stages, plus `_ensure_non_empty_main`'s floor between
    stages 4 and 5 (the module docstring's own list; see that function for why it exists), and
    return both the entries and the one number (`duplicate`) `count_indexed_pages` needs but no
    generator does. `count_indexed_pages` and `_index_entries` both call this — never a second,
    independent selection pass — so a count and the artifact it counts remain structurally
    incapable of disagreeing (module docstring, "Four public functions").
    """
    candidates = _candidates(pages, origin)
    scored = _score_all(candidates, signals)
    deduped, duplicate = _dedupe(scored)
    main, optional = _partition(deduped)
    main, optional = _ensure_non_empty_main(main, optional)
    main = _consolidate(main)
    boilerplate = _boilerplate_descriptions([candidate for candidate, _score in (*main, *optional)])
    entries = _ordered_entries(main, optional, boilerplate)
    return _Selection(entries=entries, duplicate=duplicate)


def _candidates(pages: list[CrawledPage], origin: str) -> list[_Candidate]:
    """Stage 1: filter to on-origin, 2xx, non-empty pages; sort by URL (the determinism
    contract every later stage relies on without re-asserting it); label each page; assign a
    provisional section; flag whether an always-Optional rule already matches it.

    ## Three reasons a fetched page is left out — unchanged since PER-179

    `is_empty` is the oldest: a bullet naming a URL with no title and no description tells a
    reader nothing a sitemap would not. The other two share a justification — **an artifact
    that claims to index `origin` must not list a page that is not a readable page of
    `origin`**: a `404`/`429`/`5xx` response is a real document with a real `<title>`, and a
    page whose final URL is another host is a page about another site (see `_origin`'s own
    "claude.com" regression). **Both are also enforced upstream, in `internals/crawler.py`**,
    and restating them here is the artifact asserting its own invariant over any `pages` list
    it is handed — including this module's own hand-built test fixtures — not a second
    implementation of the crawler's filter.
    """
    origin_key = _origin_key(origin)
    ordered = sorted(pages, key=_ordering_key)
    candidates = []
    for page in ordered:
        if page.is_empty or not (200 <= page.status < 300) or _origin_key(page.url) != origin_key:
            continue
        label = _label_for(page)
        candidates.append(
            _Candidate(
                page=page,
                url=page.url,
                label=label,
                section=_section_for(page.url, origin),
                always_optional=_is_always_optional(page.url),
                depth=len(_path_segments(page.url)),
                fingerprint=_fingerprint(page.markdown),
            )
        )
    return candidates


def _score_all(
    candidates: list[_Candidate], signals: Mapping[str, PageSignals]
) -> list[tuple[_Candidate, float]]:
    """Stage 2: score every candidate. The `lastmod` earliest/latest window is computed ONCE
    here, over this call's own `candidates`, and passed to every `_rank` call — never
    recomputed per candidate and never recomputed after dedup (see `_recency_fraction` and the
    module docstring)."""
    lastmods = [signals.get(candidate.url, _NO_SIGNALS).lastmod for candidate in candidates]
    known = [_to_utc(lastmod) for lastmod in lastmods if lastmod is not None]
    earliest = min(known) if known else None
    latest = max(known) if known else None
    return [
        (
            candidate,
            _rank(candidate, signals.get(candidate.url, _NO_SIGNALS), earliest, latest),
        )
        for candidate in candidates
    ]


def _dedupe(scored: list[tuple[_Candidate, float]]) -> tuple[list[tuple[_Candidate, float]], int]:
    """Stage 3: collapse candidates sharing a `fingerprint`, keeping the highest-ranked one.

    Walks a COPY of `scored`, sorted `(-score, depth, url)` — the spec's "keep the
    highest-ranked entry, tie-broken by shallower path then alphabetically-first URL" (§7)
    expressed as a sort rather than a comparison chain, so no set/dict iteration order is ever
    observable in the result. The first occurrence of each `fingerprint` in that order survives;
    every later one is counted as a duplicate and dropped. Runs BEFORE `_partition`'s cap:
    deduping after would let a duplicate consume a main-body slot a distinct page could have
    had.

    Returns:
        The survivors, in the same `(-score, depth, url)` order they were walked in, and how
        many candidates were dropped as duplicates of an already-kept survivor.
    """
    ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0].depth, pair[0].url))
    survivors: list[tuple[_Candidate, float]] = []
    seen: set[str] = set()
    duplicates = 0
    for candidate, score in ranked:
        if candidate.fingerprint in seen:
            duplicates += 1
            continue
        seen.add(candidate.fingerprint)
        survivors.append((candidate, score))
    return survivors, duplicates


def _partition(
    deduped: list[tuple[_Candidate, float]],
) -> tuple[list[tuple[_Candidate, float]], list[tuple[_Candidate, float]]]:
    """Stage 4: split `deduped` into the main body and Optional.

    A single rank-ordered walk (`(-score, url)` — the general tie-break every stage past dedup
    uses), not two separate passes over always-Optional rules and then the cap: an
    always-optional candidate never occupies a main-body slot (the `not candidate.
    always_optional` guard below), and `ALWAYS_OPTIONAL_PENALTY`'s arithmetic (see that
    constant) already guarantees such a candidate sorts at the very bottom of this same order —
    so expressing "always-Optional rules first, then rank down to the cap" (spec §4) as one
    walk with one guard produces an identical result to running them as two stages, with less
    code to keep in sync. The FIRST `MAX_MAIN_BODY_LINKS` non-always-optional candidates in
    rank order become the main body; everything else — the always-optional candidates AND any
    non-always-optional overflow past the cap — becomes Optional, still in rank order.
    """
    ranked = sorted(deduped, key=lambda pair: (-pair[1], pair[0].url))
    main: list[tuple[_Candidate, float]] = []
    optional: list[tuple[_Candidate, float]] = []
    for candidate, score in ranked:
        if not candidate.always_optional and len(main) < MAX_MAIN_BODY_LINKS:
            main.append((candidate, score))
        else:
            optional.append((candidate, score))
    return main, optional


def _ensure_non_empty_main(
    main: list[tuple[_Candidate, float]], optional: list[tuple[_Candidate, float]]
) -> tuple[list[tuple[_Candidate, float]], list[tuple[_Candidate, float]]]:
    """A FLOOR to `_partition`'s ceiling, run immediately after it: if every surviving
    candidate matched an always-Optional rule (a site that is entirely legal pages and
    archives, or a small site the rules happen to sweep entirely), `_partition` alone would
    leave `main` empty and produce H1, blockquote, straight to `## Optional` — which reads as
    a broken document, not a curated one, and is exactly the acceptance failure this function
    exists to prevent.

    A no-op whenever `main` already has at least one entry, or `optional` has none to promote.
    Otherwise, promotes the FIRST `MAX_MAIN_BODY_LINKS` candidates off the front of `optional`
    — already in rank order from `_partition` — into `main`, exactly the "highest-ranked
    entries" the promotion is meant to surface, regardless of whether they matched an
    always-Optional rule; a run that is entirely legal pages and archives still deserves a main
    body naming its best few pages rather than none at all.
    """
    if main or not optional:
        return main, optional
    return optional[:MAX_MAIN_BODY_LINKS], optional[MAX_MAIN_BODY_LINKS:]


def _consolidate(main: list[tuple[_Candidate, float]]) -> list[tuple[_Candidate, float]]:
    """Stage 5: fold a DERIVED section with fewer than `_MIN_SECTION_SIZE` entries into
    `_OTHER_SECTION`. Runs AFTER `_partition`, deliberately — demoting entries to Optional is
    what can shrink a section from several entries down to one, and this is what stops that
    shrunken section rendering as its own heading. Never touches Optional (which renders flat,
    with no sub-headings, regardless of any candidate's `section`) and never folds a canonical
    section (`_CANONICAL_SECTION_NAMES`) or `_OTHER_SECTION` itself, no matter how few entries
    either has.
    """
    counts = Counter(candidate.section for candidate, _score in main)
    consolidated: list[tuple[_Candidate, float]] = []
    for candidate, score in main:
        if (
            candidate.section not in _CANONICAL_SECTION_NAMES
            and candidate.section != _OTHER_SECTION
            and counts[candidate.section] < _MIN_SECTION_SIZE
        ):
            candidate = replace(candidate, section=_OTHER_SECTION)
        consolidated.append((candidate, score))
    return consolidated


def _ordered_entries(
    main: list[tuple[_Candidate, float]],
    optional: list[tuple[_Candidate, float]],
    boilerplate: frozenset[str],
) -> list[_Entry]:
    """Stage 7 (stage 6, `_describe`, happens per-entry inline here — see the module docstring):
    the main body sorted `(section rank, section name, -score, url)`, then Optional sorted flat
    `(-score, url)`, concatenated. This is the one list `generate_llms_txt`'s render loop,
    `_plan_full_txt`, and `count_indexed_pages` all consume — see the module docstring's "Four
    public functions" section for why sharing this list, rather than recomputing it, is what
    keeps a count and its artifact from disagreeing.
    """
    ordered_main = sorted(
        main,
        key=lambda pair: (_section_rank(pair[0].section), pair[0].section, -pair[1], pair[0].url),
    )
    ordered_optional = sorted(optional, key=lambda pair: (-pair[1], pair[0].url))

    entries = [
        _Entry(
            section=candidate.section,
            url=candidate.url,
            label=candidate.label,
            description=_describe(candidate.page.description, candidate.label, boilerplate),
            markdown=candidate.page.markdown,
            optional=False,
        )
        for candidate, _score in ordered_main
    ]
    entries.extend(
        _Entry(
            section=OPTIONAL_SECTION,
            url=candidate.url,
            label=candidate.label,
            description=_describe(candidate.page.description, candidate.label, boilerplate),
            markdown=candidate.page.markdown,
            optional=True,
        )
        for candidate, _score in ordered_optional
    )
    return entries


# --- shared, unchanged since before this ticket -------------------------------------------------


def _ordering_key(page: CrawledPage) -> tuple[str, str, str, str]:
    """The total order every pass over `pages` in this module uses.

    URL first — the determinism contract the module docstring states. The three fields after
    it only ever break a tie between two pages that share a URL, which `crawl_site` cannot
    produce (its frontier visits each URL once) but a hand-built list can.

    Shared by `_candidates` and `_root_page` rather than written out at each call site, because
    the two disagreeing is the specific bug this function exists to prevent: an earlier
    revision keyed `_project_name` on the URL alone, and two root pages with different titles
    produced a different H1 depending on which one the caller happened to list first.
    """
    return (page.url, page.title or "", page.description or "", page.markdown)


def _label_for(page: CrawledPage) -> str:
    """What to call `page` in a link and in an H2 heading: its extracted title, or its last
    path segment humanized, or the host.

    Never the raw URL: the URL is already the link's target, and `- [https://…](https://…)` is
    noise rather than information. A page with no title and no path segment is the site root,
    where the host is the only thing left that names it. A trailing filename has its extension
    dropped first, so `/docs/intro.html` reads "Intro" rather than "Intro.html".
    """
    title = _clean(page.title)
    if title:
        return title
    segments = _path_segments(page.url)
    if segments:
        # Through `_clean` as well, not just the title above: a path segment is as much the
        # crawled site's choice as a `<title>` is, so the fallback needs the same bound.
        label = _clean(_humanize(segments[-1].rsplit(".", 1)[0]))
        if label:
            return label
    return _clean(urlsplit(page.url).netloc) or urlsplit(page.url).netloc


def _origin(site_url: str) -> str:
    """The scheme and host this artifact is about, from the caller's `site_url`.

    **This used to be `min(page.url for page in pages)` — the alphabetically first URL the run
    fetched — and that is the bug `site_url` was added to the seam to fix.** It rested on
    "every page in this run shares an origin," which is true of what the crawler REQUESTS and
    was never true of what it collects: `CrawledPage.url` is the final url after redirects, so
    a single page answering from another host was enough to rename the whole document.

    A derived origin can be outvoted by the data it is derived from. A given one cannot.
    """
    parts = urlsplit(site_url)
    return f"{parts.scheme}://{parts.netloc}"


def _origin_key(url: str) -> str:
    """The MEMBERSHIP form of an origin: scheme and host with a leading `www.` folded away.

    Distinct from `_origin` above, which is the DISPLAY form and keeps the host exactly as the
    run landed on it. What folds is only the comparison. An apex page and a `www` page belong
    to one site (see `internals/url_ranking.py`'s `strip_www`), so an artifact built for either
    spelling lists both rather than silently dropping half its own pages.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{strip_www(parts.netloc)}"


def _project_name(root_page: CrawledPage | None, origin: str) -> str:
    """The H1: `root_page`'s own title if it has a usable one, else `origin`.

    Takes an already-resolved `root_page` (see `_root_page`) rather than scanning `pages`
    itself — the module docstring's "seven stages" section, and `_root_page`'s own docstring,
    explain why one shared resolution beats two independent ones. Falling back to `origin` is
    honest: a bare host is obviously an identifier, where a mislabelled H1 is not obviously
    anything.
    """
    if root_page is not None:
        title = _clean(root_page.title)
        if title:
            return title
    return origin


def _bullet(entry: _Entry) -> str:
    """One `- [label](url): description` line, with the description omitted entirely rather
    than left as a dangling `: ` when the page has none. A description is optional in the
    llmstxt.org shape, and an empty one is not a different thing from an absent one.

    **This format has a reverse parser.** `internals/index_diff.py`'s `parse_index` recovers
    an `IndexEntry` from exactly this line shape, to diff one run's index against the
    previous one's (PER-193). The two are two descriptions of one format and can drift apart
    silently if either changes without the other —
    `tests/test_index_diff.py::test_parse_index_round_trips_generate_llms_txt` is the one
    test tying them together; touching this function or `_escape_label`/`_escape_target`
    without re-running it is how that drift happens unnoticed.
    """
    link = f"- [{_escape_label(entry.label)}]({_escape_target(entry.url)})"
    return f"{link}: {entry.description}" if entry.description else link


def _escape_label(label: str) -> str:
    for character, escaped in _LABEL_ESCAPES:
        label = label.replace(character, escaped)
    return label


def _escape_target(url: str) -> str:
    for character, escaped in _TARGET_ESCAPES:
        url = url.replace(character, escaped)
    return url


def _clean(text: str | None) -> str | None:
    """Collapse every whitespace run in `text` to one space, or return `None`.

    A title or description is emitted inside a single markdown line, and extraction reads both
    out of markup where a newline is insignificant — a `<title>` split across three indented
    source lines is one title. Left alone, that newline would end the bullet and turn the rest
    of the title into a stray paragraph.

    Returns `None`, never `""`, for both an absent value and one that was entirely whitespace,
    so callers can keep writing `_clean(x) or fallback` — the same reason
    `ExtractedContent.title` is `None` rather than empty.

    Also the one place a title or description is bounded, at `MAX_TEXT_CHARS` — see that
    constant for why an unbounded one is a real problem rather than an untidy one. Every value
    this module puts in an H1, an H2 or a bullet passes through here, which is what makes one
    check sufficient.
    """
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_TEXT_CHARS:
        return collapsed[:MAX_TEXT_CHARS].rstrip() + _TEXT_ELLIPSIS
    return collapsed or None
