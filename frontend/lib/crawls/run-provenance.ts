// The view model behind the Output tab's "Show how this was built" panel
// (components/crawls/crawl-provenance.tsx) — one run's `stats` turned into the four stages
// the ticket asks for (Discovery, Selection, Fetch, Index), plus the five states its own
// numbers can be in (unavailable, seed-never-fetched, seed-only, totals-only, a real
// breakdown).
//
// Reads `run.stats` the same defensive `?.`/`typeof`/`Number.isFinite` way `run-display.ts`'s
// `runPagesCrawled` does, for the identical reason: `stats` is jsonb whose shape belongs to
// the crawler milestone (ARCHITECTURE.md §3.4), typed loosely on the wire
// (`{ [key: string]: unknown } | null`), so a client reads it defensively rather than
// assuming a shape that may not be there. `RunProvenance` below is this file's own derived
// type, the same relationship `stats-display.ts`'s `TrendPoint` has to the raw API response —
// a shape this module builds, not one the backend returns.
//
// The rule labels and one-line explanations are NOT here. This module owns the numbers and
// the five-state predicate; `lib/crawls/provenance-copy.ts` owns every string a human wrote —
// `SELECTION_RULE_ORDER`, `selectionRuleCopy`, `DISCOVERY_SOURCE`, `CAP_HIT` — the same split
// `enrichment-copy.ts` vs `run-display.ts` already draws for the Runs and Output tabs'
// enrichment badge (ARCHITECTURE.md §8.4).

import type { RunDetail } from "@/lib/api/runs";
import { SELECTION_RULE_ORDER } from "@/lib/crawls/provenance-copy";
import { runPagesCrawled } from "@/lib/crawls/run-display";

/** One row of the Selection funnel: a rule key (`SelectionRuleKey`, or an unrecognised string
 * this panel has not shipped copy for yet — see `selectionRuleCopy`'s degrade path) and how
 * many candidates it dropped. */
export interface SelectionRow {
  key: string;
  count: number;
}

/**
 * The five states `runs.stats` can put the Selection stage in — see the ticket's own States
 * section, and the acceptance criterion this type exists to make impossible to get wrong:
 * never a zero standing in for "unknown," and never two stages of the same panel disagreeing
 * about what happened.
 *
 * * `"seed_not_fetched"` — discovery found nothing (`urlsDiscovered === 0`) AND the seed
 *   itself never landed (`!seedFetched`, i.e. `pagesCrawled === 0` — `internals/crawler.py`'s
 *   own comment that an empty `pages` list is exactly equivalent to "the seed never landed").
 *   A domain that is entirely down, not a page that was merely light on links. Distinct from
 *   `"seed_only"` on purpose: this state makes NO claim that the seed was crawled, because it
 *   was not — see `test_a_failed_seed_reports_no_discovery_source_even_with_the_fallback_armed`
 *   (`backend/tests/test_run_persistence.py`) for the row that would otherwise render "the run
 *   crawled the seed alone" in this same panel's Selection section, directly above a Fetch
 *   section reading "Fetched: 0."
 * * `"seed_only"` — discovery found nothing (`urlsDiscovered === 0`) but the seed WAS fetched
 *   (`seedFetched`): a sitemap that 404'd, 404'd again, and a seed page with no links either.
 *   The funnel is one row, not an empty table.
 * * `"breakdown"` — a real funnel: every rule that fired, in `SELECTION_RULE_ORDER`, plus
 *   `selected`, the frontier `crawl_site` actually got. Reachable even when the seed itself
 *   later failed to fetch (sitemap discovery runs before the seed is fetched — see
 *   `service.py`'s `execute_run`) — that combination is not this type's problem to flag: it
 *   makes no "the seed was crawled" claim, so nothing here contradicts the Fetch stage.
 * * `"totals_only"` — `stats.dropped` is not a plain object at all, which per
 *   `RUN_STATS_VERSION`'s version-9 paragraph means exactly one thing and never any other:
 *   **this row predates version 9.** The per-rule split is gone, but the two ends of the
 *   funnel are not — `urls_discovered` and `urls_selected` have been recorded since version 4
 *   and `urls_robots_disallowed` since version 6, so the total dropped is
 *   `discovered - selected` and, on a version-6-or-later row, one named rule survives with it.
 *   Degrading to that is the difference between "we did not record which rules fired" and
 *   "we know nothing about selection," and only the first is true. This is the state every run
 *   crawled before PER-196 deployed lands in — which, at the time it shipped, was every run in
 *   the database.
 * * `"unavailable"` — no breakdown AND no totals either: a row so old (or so truncated) that
 *   `urls_discovered`/`urls_selected` are missing too. The only state that renders no numbers,
 *   and the only one that should be genuinely rare.
 */
export type SelectionState =
  | { kind: "unavailable" }
  | { kind: "seed_not_fetched" }
  | { kind: "seed_only" }
  | {
      kind: "totals_only";
      discovered: number;
      selected: number;
      droppedTotal: number;
      /** `stats.urls_robots_disallowed` — the one rule whose count survives on a version-6-to-8
       * row, or `null` on a version-4/5 row that never recorded it. */
      robotsDisallowed: number | null;
    }
  | { kind: "breakdown"; rows: SelectionRow[]; droppedTotal: number; selected: number };

/** What reached (or never reached) `llms.txt` — `"not_stored"` when this run never persisted
 * one, `"stored"` with the SIX counts otherwise (four omission reasons, plus a listed-not-
 * dropped bucket and a fourth omission reason, both new at `RUN_STATS_VERSION` 13 — see
 * `runProvenance`'s own docstring for the invariant all six now close). See `runProvenance`'s
 * own docstring for why the gate is `run.status === "completed"` rather than "does
 * `stats.links_emitted` exist." */
export type IndexState =
  | { kind: "not_stored" }
  | {
      kind: "stored";
      indexed: number;
      omittedEmpty: number;
      /** `stats.links_optional` (`RUN_STATS_VERSION` 13) — pages the curated index demoted to
       * `## Optional` rather than the main body: still LISTED, still in `llms.txt` and
       * `llms-full.txt`, just not in the curated top. `null`, NEVER `0`, on a pre-version-13
       * row — that row's `llms.txt` had no Optional concept at all, so "how many pages were
       * listed under Optional" has no answer to give, and `0` would silently claim a row from
       * before this concept existed had an Optional section with nothing in it. See `runProvenance`'s
       * docstring for why this field does NOT use the `?? 0` idiom the other five here do. */
      listedOptional: number | null;
      /** `stats.links_duplicate` (`RUN_STATS_VERSION` 13) — fetched, on-origin, non-empty, 2xx
       * pages this run's own dedup pass collapsed into an already-kept survivor: fetched and
       * real, but not a link anywhere in the artifact. `null`, NEVER `0`, on a pre-version-13
       * row, for the identical reason `listedOptional` is — a row from before dedup existed
       * has no answer to "how many pages did dedup collapse," and `0` would claim one. */
      omittedDuplicate: number | null;
    };

/**
 * The page budget this run ran under — `stats.max_pages`, and what ranking was therefore
 * allowed to select.
 *
 * **This is the number that makes the Selection stage readable rather than merely accurate.**
 * "99 URLs selected, 252 dropped" invites exactly one wrong reading — that the 252 failed some
 * quality bar — and the right reading is only available with the budget beside it: 351
 * candidates passed every rule, ranking sorted them, and the top 99 fit. There is no
 * threshold anywhere in `url_ranking.py`'s walk; there is a queue and a cut-off.
 *
 * `source` records where the number came from, because the two are not equally available:
 *
 * * `"recorded"` — `stats.max_pages`, a `RUN_STATS_VERSION` 10 key (PER-201). Always right,
 *   and present whether or not the budget actually bound.
 * * `"derived"` — a version-9-or-earlier row, reconstructed as `urlsSelected + 1`.
 *   `select_urls` is called with `limit = max_pages - 1` and its `"over_limit"` rule fires
 *   only once `len(selected)` has reached that limit, so on a row where `over_limit` fired,
 *   `urlsSelected === max_pages - 1` exactly. **This derivation is available only on such a
 *   row** — on any other, `urlsSelected` is just how many URLs the site happened to have, and
 *   `+ 1` would report a budget of 3 for a run that really had 100. That one-sidedness is why
 *   version 10 records the number rather than leaving every reader to reimplement a rule that
 *   silently has no answer half the time, and why `pageBudget` is `null` rather than a guess
 *   on an old row whose budget never bound.
 */
export interface PageBudget {
  /** The whole budget, seed included — `Settings.crawl_max_pages`. */
  maxPages: number;
  /** What ranking was allowed to select: `maxPages - 1`. The seed is fetched separately and
   * already counts against the budget (`url_ranking.py`'s own `limit` parameter), so this,
   * not `maxPages`, is the number `urlsSelected` should be read against. */
  frontierLimit: number;
  source: "recorded" | "derived";
}

/** The Fetch stage's numbers — see `runProvenance`'s docstring for the seed/frontier split and
 * which inequalities involving these fields hold and which do not. */
export interface FetchInfo {
  /** Whether the seed itself was fetched — `pagesCrawled > 0`. */
  seedFetched: boolean;
  /** Pages fetched from the ranked frontier, NOT counting the seed, and NOT counting a
   * blocked page — `internals/crawler.py`'s frontier loop counts a blocked page on `blocked`
   * below instead of appending it to `CrawlResult.pages`, so it never reaches
   * `pages_crawled` at all. */
  frontierFetched: number;
  failed: number;
  /** `stats.pages_blocked` (`RUN_STATS_VERSION` 11) — how many of this run's fetched pages,
   * seed included, were a detected WAF/CDN access challenge or denial
   * (`internals/blocked.py`). `0` when this row predates version 11 or nothing was blocked;
   * see `blockedReason` below for which kind. Render this row only when it is `> 0`, the same
   * "zero segments are dropped from the legend" rule every other Fetch segment already
   * follows. */
  blocked: number;
  /** `stats.blocked_reason` — `"challenge"` or `"denied"`, or `null` when nothing this run
   * fetched was blocked (`blocked === 0`) or the row predates version 11. A SEED block fails
   * the run outright, so on a `failed` run this is the reason the run failed, not merely a
   * frontier-page count; `lib/crawls/provenance-copy.ts`'s `blockedReasonCopy` turns it into
   * a sentence. */
  blockedReason: string | null;
  /** `stats.pages_http_error` (`RUN_STATS_VERSION` 12), attributed to the FRONTIER — a
   * selected URL that answered with an honest `404`, `429` or `5xx`, one
   * `internals/blocked.py` declined to call an access block. A Fetch-stage number rather than
   * an Index-stage one, and that distinction is the whole point: `internals/crawler.py`
   * counts one of these in place of `pages.append`, so it never reaches `pages_crawled` and
   * the Index stage has nothing to say about it. It is a selected URL that WAS attempted and
   * yielded no page — exactly the shape `blocked` already has.
   *
   * **Frontier-attributed, because the stored counter is not.** `pages_http_error` also
   * counts a SEED that answered non-2xx, and the seed is never a member of `selected`, so
   * subtracting the raw counter from `urlsSelected` would undercount by one on exactly those
   * runs. `seedFetched` separates the two cleanly and without a new stored key: a seed that
   * landed was a 2xx by construction (a non-2xx seed is counted and then never appended), so
   * when `seedFetched` is true every one of these came from the frontier; and when it is
   * false the frontier never ran at all — it lives inside the seed fetch's own `else` branch
   * — so none of them did. */
  httpError: number;
  /** `stats.pages_off_origin` (`RUN_STATS_VERSION` 12) — a selected URL that was requested on
   * this site and answered by a different host. Counted in place of `pages.append` exactly as
   * `httpError` is, and read the same way. Frontier-only at the source, so it needs no
   * attribution rule: the seed is deliberately exempt from the redirect check
   * (`_redirected_off_origin` — a site that moved wholesale is crawled where it moved to). */
  offOrigin: number;
  /** Selected but never attempted — nonzero only when a cap ended the run before the whole
   * frontier was fetched. Render this row only when it is `> 0`.
   *
   * **This is the residual, which is why every other outcome above has to be subtracted from
   * it.** It is `urlsSelected` minus everything the funnel can name, so an outcome the
   * formula does not enumerate does not go missing — it silently lands here and is rendered
   * under a label that is false of it. That has now happened twice: to `blocked` at version
   * 11, and to `httpError`/`offOrigin` at version 12. Both are enumerated above for that
   * reason, and a version 14 that adds a third had better arrive with a line here. */
  notAttempted: number;
  bytesFetched: number | null;
  /** The raw `cap_hit` value (`"pages"` | `"bytes"` | `"wall_clock"`), or `null` when no cap
   * ended this run. `lib/crawls/provenance-copy.ts`'s `CAP_HIT`/`NO_CAP_HIT` turn this into a
   * sentence; this field is deliberately just the key. */
  capHit: string | null;
}

/** One run's provenance, derived from `run.stats` — everything
 * `components/crawls/crawl-provenance.tsx` renders. */
export interface RunProvenance {
  discoverySource: string | null;
  urlsDiscovered: number | null;
  selection: SelectionState;
  /** This run's page budget, or `null` when the row neither records nor allows deriving one
   * — see `PageBudget`. */
  pageBudget: PageBudget | null;
  /** How many candidates the `"over_limit"` rule dropped, or `null` on a row with no per-rule
   * split to read it from.
   *
   * Broken out of `selection.rows` rather than left for the renderer to find, because it is
   * the one rule two different stages need: it is what tells Selection that the budget was the
   * binding constraint, and it is what tells Fetch that `cap_hit: null` does NOT mean the run
   * had budget to spare (`runProvenance`'s docstring, "The cap the Fetch stage cannot see").
   * A `0` here is a real recorded zero — the rule did not fire — and is distinct from `null`.
   */
  overLimit: number | null;
  fetch: FetchInfo;
  index: IndexState;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * This run's page budget: `stats.max_pages` when the row records one, the `urlsSelected + 1`
 * reconstruction when it does not but `"over_limit"` fired, `null` otherwise.
 *
 * See `PageBudget`'s own docstring for why the fallback is valid in exactly one case and
 * silently wrong everywhere else — that asymmetry is the entire reason this function returns
 * `null` rather than a best effort. A budget rendered as "100 pages" when it was really 3 is
 * worse than no budget rendered at all: the first is a wrong answer to the question the panel
 * was opened to ask, and the second is the panel declining to answer it.
 *
 * `maxPages < 1` is refused rather than clamped. `Settings.crawl_max_pages` is `ge=1`, so a
 * row carrying less is a row this module does not understand, and the honest response to that
 * is `null` — the same reasoning `selectionState` applies to a `dropped` value that is not a
 * plain object.
 */
function pageBudget(
  stats: Record<string, unknown>,
  urlsSelected: number | null,
  overLimit: number | null
): PageBudget | null {
  const recorded = finiteNumber(stats.max_pages);
  if (recorded !== null && recorded >= 1) {
    return { maxPages: recorded, frontierLimit: recorded - 1, source: "recorded" };
  }
  if (urlsSelected !== null && overLimit !== null && overLimit > 0) {
    return { maxPages: urlsSelected + 1, frontierLimit: urlsSelected, source: "derived" };
  }
  return null;
}

function selectionState(
  stats: Record<string, unknown>,
  urlsDiscovered: number | null,
  urlsSelected: number | null,
  seedFetched: boolean
): SelectionState {
  // "Discovery found nothing" is checked FIRST, ahead of the breakdown, because it is the same
  // story whether or not the per-rule map is there: a run with no candidates has no funnel to
  // split up, and saying "the breakdown isn't available" about it would withhold an answer this
  // module already has. Only a run that discovered something needs to know how the split was
  // recorded.
  if (urlsDiscovered === 0) {
    // Both branches are "the funnel is one row, not an empty table" — which row depends on
    // whether the seed itself landed. Claiming "crawled the seed alone" when the seed never
    // fetched is the exact contradiction `"seed_not_fetched"` exists to rule out; see this
    // type's own docstring above.
    return seedFetched ? { kind: "seed_only" } : { kind: "seed_not_fetched" };
  }

  const dropped = stats.dropped;
  // Pre-version-9 rows simply have no `dropped` key; `typeof null === "object"` is why `null`
  // is excluded explicitly rather than by `typeof` alone, and the array check guards against
  // a value that is technically an object but not the `{rule: count}` map this key promises.
  if (dropped === null || typeof dropped !== "object" || Array.isArray(dropped)) {
    // No per-rule split — but `urls_discovered` and `urls_selected` are version-4 keys, so the
    // two ENDS of the funnel are still on the row, and `discovered - selected` is the total
    // dropped by construction (`url_ranking.py`'s reconciliation invariant, which every version
    // since 4 has held). Rendering that, rather than a flat "not available," is the whole point
    // of this branch: what is missing is which rules fired, not whether any did. `Math.max(0, …)`
    // is the same clamp `notAttempted` below uses, against a row whose two totals disagree.
    if (urlsDiscovered === null || urlsSelected === null) return { kind: "unavailable" };
    return {
      kind: "totals_only",
      discovered: urlsDiscovered,
      selected: urlsSelected,
      droppedTotal: Math.max(0, urlsDiscovered - urlsSelected),
      robotsDisallowed: finiteNumber(stats.urls_robots_disallowed),
    };
  }

  const droppedMap = dropped as Record<string, unknown>;
  const rows: SelectionRow[] = [];
  const seen = new Set<string>();

  // `SELECTION_RULE_ORDER` first — this IS the jsonb-key-order workaround the ticket's finding
  // (b) requires: the stored map's own key order is not `_RULE_ORDER` once `jsonb` has
  // reordered it, so the render loop is driven by this module's own ordered constant instead
  // of `Object.entries(droppedMap)`.
  for (const key of SELECTION_RULE_ORDER) {
    seen.add(key);
    const count = finiteNumber(droppedMap[key]);
    if (count !== null && count > 0) {
      rows.push({ key, count });
    }
  }

  // Then any key this panel does not yet have copy for — a rule the backend shipped after
  // this table was last updated — appended in whatever order the stored map happens to
  // deserialize in. `selectionRuleCopy` (provenance-copy.ts) is what keeps this from rendering
  // blank: an unrecognised key renders as itself.
  for (const [key, value] of Object.entries(droppedMap)) {
    if (seen.has(key)) continue;
    const count = finiteNumber(value);
    if (count !== null && count > 0) {
      rows.push({ key, count });
    }
  }

  const droppedTotal = rows.reduce((sum, row) => sum + row.count, 0);
  return { kind: "breakdown", rows, droppedTotal, selected: urlsSelected ?? 0 };
}

/**
 * How many URLs this run's ranking actually kept, or `null` when the row does not say.
 *
 * The Selection stage's own headline number, and — because the funnel bars are drawn to one
 * shared scale — a number the Fetch stage needs too. Written once here rather than as a
 * `switch` inside the renderer so the two stages can never disagree about it: `"seed_only"` and
 * `"seed_not_fetched"` both mean zero selected (discovery found nothing to select FROM), and
 * only `"unavailable"` genuinely does not know.
 */
export function selectionSelected(selection: SelectionState): number | null {
  switch (selection.kind) {
    case "unavailable":
      return null;
    case "seed_not_fetched":
    case "seed_only":
      return 0;
    case "totals_only":
    case "breakdown":
      return selection.selected;
  }
}

/**
 * `run.stats` turned into the four-stage view model the provenance panel renders, or `null`
 * when there is nothing to render at all (`stats` itself absent — a run this loosely typed
 * jsonb column has never described in any version).
 *
 * ## The invariants this module's numbers hold, and the one they deliberately do not
 *
 * * `urlsDiscovered - sum(dropped) === urlsSelected` holds by construction whenever
 *   `selection.kind === "breakdown"` — `url_ranking.py`'s own reconciliation invariant,
 *   carried through unchanged.
 * * `indexed <= pagesCrawled` holds: the artifact can only list pages this run actually
 *   fetched.
 * * `urlsSelected >= frontierFetched` holds — the frontier was never asked to fetch more than
 *   it selected. **`urlsSelected >= pagesCrawled` does NOT hold**, and the seed is why:
 *   `pages_crawled` (`internals/crawler.py`) counts the seed fetch, which is never a member of
 *   `selected` (`url_ranking.py`'s own docstring — the seed is dropped under its own `"seed"`
 *   rule and fetched separately, before ranking ever runs). This is the ticket's own finding
 *   (a): the seed has to be its own visible term in the funnel, or "selected -> fetched" reads
 *   as broken arithmetic. `frontierFetched` below is `pagesCrawled - 1` for exactly this
 *   reason — it excludes the seed so the comparison against `urlsSelected` is honest.
 * * `indexed + listedOptional + omittedEmpty + omittedDuplicate === pagesCrawled` is the
 *   Index stage's reconciliation, and every one of its four terms is a fact about a page that
 *   IS in `pages_crawled`. `pages_empty_content` is counted over `CrawlResult.pages`
 *   (`internals/crawler.py`), and `links_emitted`/`links_optional`/`links_duplicate` all come
 *   from one `_select` pass over that same list (`internals/llms_txt.py`'s
 *   `count_indexed_pages`), whose only exclusion that can still bite there is `is_empty` —
 *   non-2xx and off-origin are already gone before the list is built.
 *
 *   **`pages_http_error` and `pages_off_origin` are deliberately NOT terms here, and used to
 *   be.** They read as index omissions, but `internals/crawler.py` counts each one in place of
 *   `pages.append` — `run_stats.py` says it outright: "excluded from `pages_crawled` as well as
 *   from the index." Summing them into a total whose denominator they were never part of
 *   overshot it by exactly their own count, and the same two pages were then drawn a second
 *   time in the Fetch stage's residual. They are Fetch-stage facts about a URL that was
 *   attempted, and they now live there — see `FetchInfo.httpError`.
 *
 *   Every term is an independently recorded fact rather than one derived from the others,
 *   matching `links_emitted`'s own docstring: "ask the artifact what it listed; do not
 *   reconstruct it." On a pre-version-13 row, `listedOptional` and `omittedDuplicate` are both
 *   `null` rather than `0` — see `IndexState`'s own docstring — so the four-term sum is not
 *   evaluable on such a row; `indexed + omittedEmpty === pagesCrawled` is what holds for it.
 *
 * ## Fetch
 *
 * `seedFetched = pagesCrawled !== null && pagesCrawled > 0` — justified by
 * `internals/crawler.py`'s own comment on why an empty `pages` list is exactly equivalent to
 * "the seed never landed": every later append happens after a successful seed fetch, so there
 * is no other way to end up with pages fetched at all without the seed being one of them.
 * `frontierFetched = Math.max(0, pagesCrawled - 1)`. **`fetch_frontier_url`
 * (`internals/crawler.py`) has FOUR ways to spend a selected URL without appending a page,
 * and `notAttempted` is the residual, so all four have to be named or the unnamed ones land
 * in it wearing a label that is false of them.** An exception counts on `pages_failed`; a
 * detected WAF challenge on `pages_blocked` (version 11); an honest non-2xx on
 * `pages_http_error` and a cross-origin redirect on `pages_off_origin` (version 12). All four
 * share one shape — counted, and then returned from in place of `pages.append`, so none of
 * them reaches `pages_crawled`. Hence
 *
 * ```
 * notAttempted = max(0, urlsSelected - frontierFetched - failed - blocked - httpError - offOrigin)
 * ```
 *
 * every selected URL accounted for by exactly one of frontier-fetched, failed, blocked,
 * not-a-page, another-site, or not-attempted. Nonzero only when a cap ended the run before the
 * whole selected frontier was attempted — render that row only when it is greater than zero.
 *
 * **The last two terms were missing, and a live run is what found it.** Version 11 added
 * `blocked` to this formula for exactly this reason; version 12 added two more counters of the
 * same shape and did not. A 100-page run that selected 99 URLs, fetched 97 of them, and had
 * two answer with a non-2xx reported "Not attempted 2" under a note reading "Every page this
 * run selected was fetched" — two pages that were attempted, described as not attempted,
 * beside a sentence saying they were. Adding a counter to `internals/crawler.py` without
 * adding it here does not produce a gap in this panel; it produces a confident wrong number.
 *
 * `Math.max(0, …)` throughout is a clamp against a stats row whose numbers disagree, the same
 * clamp rationale `stats-display.ts`'s `outcomeBreakdown` gives for its own remainder.
 *
 * ## The cap the Fetch stage cannot see
 *
 * **`cap_hit: null` does not mean the run had budget to spare, and on a site with more pages
 * than the budget it never will.** `crawl_site` records `cap_hit: "pages"` from exactly two
 * places (`internals/crawler.py`), and a run whose page budget was fully consumed reaches
 * neither:
 *
 * * The loop's own truncation sets it when the frontier it was handed is longer than
 *   `max_pages - 1`. But `select_urls` was already called with `limit = max_pages - 1`, so the
 *   frontier that arrives is never longer than that, and `frontier_was_truncated` is always
 *   `false`.
 * * The per-fetch guard sets it when `len(pages) >= max_pages`. With a frontier of exactly
 *   `max_pages - 1` and the seed already appended, the last frontier task runs its check when
 *   `len(pages)` is at most `max_pages - 1`. It never fires either.
 *
 * So the run that spent every page it had reports the same `cap_hit: null` as the run that
 * finished with room left over, and the panel would otherwise print "no cap was hit" directly
 * under a row reading "-252 Over the page limit." `overLimit` above is what separates the two,
 * and `provenance-copy.ts`'s `fetchCapNote` is what says so. This is a REPORTING gap, not a
 * crawler bug: `cap_hit` faithfully answers "which cap stopped the fetch loop," and the page
 * budget stopped this run one stage earlier, where that field cannot see it.
 *
 * ## Index
 *
 * `{ kind: "not_stored" }` whenever `run.status !== "completed"` — not gated on whether
 * `stats.links_diff`/`links_emitted` happen to be present, because `run_stats.py`'s
 * `build_run_stats` docstring is explicit that a FAILED row can carry fully-populated
 * artifact-shaped stats describing an index that was never actually written (the upload or the
 * final write failed after generation): "every reader of this column filters on `status =
 * 'completed'` first." This module is one of those readers.
 */
export function runProvenance(run: Pick<RunDetail, "status" | "stats">): RunProvenance | null {
  const stats = run.stats;
  if (stats === null || stats === undefined) return null;

  const discoverySource = typeof stats.discovery_source === "string" ? stats.discovery_source : null;
  const urlsDiscovered = finiteNumber(stats.urls_discovered);
  const urlsSelected = finiteNumber(stats.urls_selected);

  const pagesCrawled = runPagesCrawled(run);
  const failed = finiteNumber(stats.pages_failed) ?? 0;
  const blocked = finiteNumber(stats.pages_blocked) ?? 0;
  const blockedReason = typeof stats.blocked_reason === "string" ? stats.blocked_reason : null;
  const bytesFetched = finiteNumber(stats.bytes_fetched);
  const capHit = typeof stats.cap_hit === "string" ? stats.cap_hit : null;

  // Read off the stored map directly rather than out of `selection.rows`, so it is available
  // in the same shape whatever state selection landed in — `null`, never `0`, on a row with no
  // per-rule split, because "this row does not record which rules fired" and "this rule
  // dropped nothing" are different facts and the Fetch stage's wording below turns on which
  // one it is.
  const droppedMap = stats.dropped;
  const overLimit =
    droppedMap !== null && typeof droppedMap === "object" && !Array.isArray(droppedMap)
      ? (finiteNumber((droppedMap as Record<string, unknown>).over_limit) ?? 0)
      : null;

  const seedFetched = pagesCrawled !== null && pagesCrawled > 0;
  const frontierFetched = pagesCrawled === null ? 0 : Math.max(0, pagesCrawled - 1);
  // Frontier-attributed, never the raw counter — see `FetchInfo.httpError` for why a seed's
  // own non-2xx must not be subtracted from a total the seed was never part of.
  const httpError = seedFetched ? (finiteNumber(stats.pages_http_error) ?? 0) : 0;
  const offOrigin = finiteNumber(stats.pages_off_origin) ?? 0;
  const notAttempted =
    urlsSelected === null
      ? 0
      : Math.max(0, urlsSelected - frontierFetched - failed - blocked - httpError - offOrigin);

  const index: IndexState =
    run.status !== "completed"
      ? { kind: "not_stored" }
      : {
          kind: "stored",
          indexed: finiteNumber(stats.links_emitted) ?? 0,
          omittedEmpty: finiteNumber(stats.pages_empty_content) ?? 0,
          // NEVER `?? 0` — see `IndexState`'s own docstring for why `null` on a pre-version-13
          // row must survive as `null` rather than being read as "this run had none."
          listedOptional: finiteNumber(stats.links_optional),
          omittedDuplicate: finiteNumber(stats.links_duplicate),
        };

  return {
    discoverySource,
    urlsDiscovered,
    selection: selectionState(stats, urlsDiscovered, urlsSelected, seedFetched),
    pageBudget: pageBudget(stats, urlsSelected, overLimit),
    overLimit,
    fetch: {
      seedFetched,
      frontierFetched,
      failed,
      blocked,
      blockedReason,
      httpError,
      offOrigin,
      notAttempted,
      bytesFetched,
      capHit,
    },
    index,
  };
}

/**
 * Bytes to a short human string ("512 B", "12.3 KB", "4.1 MB") for the Fetch stage's byte
 * count. Base 1024, not 1000 — the same KiB/MiB-mirroring justification `stats-display.ts`'s
 * `bytesToKb` gives for its own unit, applied here to a single formatted value rather than a
 * chart axis's raw number.
 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;

  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}
