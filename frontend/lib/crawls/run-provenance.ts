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
 * one, `"stored"` with the two counts otherwise. See `runProvenance`'s own docstring for why
 * the gate is `run.status === "completed"` rather than "does `stats.links_emitted` exist." */
export type IndexState =
  | { kind: "not_stored" }
  | { kind: "stored"; indexed: number; omittedEmpty: number };

/** The Fetch stage's numbers — see `runProvenance`'s docstring for the seed/frontier split and
 * which inequalities involving these fields hold and which do not. */
export interface FetchInfo {
  /** Whether the seed itself was fetched — `pagesCrawled > 0`. */
  seedFetched: boolean;
  /** Pages fetched from the ranked frontier, NOT counting the seed. */
  frontierFetched: number;
  failed: number;
  /** Selected but never attempted — nonzero only when a cap ended the run before the whole
   * frontier was fetched. Render this row only when it is `> 0`. */
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
  fetch: FetchInfo;
  index: IndexState;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
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
 * * `indexed + omittedEmpty === pagesCrawled` happens to hold today (every fetched page is
 *   either indexed or empty), but this module renders both as independent recorded facts —
 *   `stats.links_emitted` and `stats.pages_empty_content` — rather than deriving one from the
 *   other, matching `links_emitted`'s own docstring: "ask the artifact what it listed; do not
 *   reconstruct it."
 *
 * ## Fetch
 *
 * `seedFetched = pagesCrawled !== null && pagesCrawled > 0` — justified by
 * `internals/crawler.py`'s own comment on why an empty `pages` list is exactly equivalent to
 * "the seed never landed": every later append happens after a successful seed fetch, so there
 * is no other way to end up with pages fetched at all without the seed being one of them.
 * `frontierFetched = Math.max(0, pagesCrawled - 1)`. `notAttempted = Math.max(0, urlsSelected -
 * frontierFetched - failed)` is nonzero only when a cap ended the run before the whole
 * selected frontier was fetched — render that row only when it is greater than zero.
 * `Math.max(0, …)` in both is a clamp against a stats row whose numbers disagree, the same
 * clamp rationale `stats-display.ts`'s `outcomeBreakdown` gives for its own remainder.
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
  const bytesFetched = finiteNumber(stats.bytes_fetched);
  const capHit = typeof stats.cap_hit === "string" ? stats.cap_hit : null;

  const seedFetched = pagesCrawled !== null && pagesCrawled > 0;
  const frontierFetched = pagesCrawled === null ? 0 : Math.max(0, pagesCrawled - 1);
  const notAttempted =
    urlsSelected === null ? 0 : Math.max(0, urlsSelected - frontierFetched - failed);

  const index: IndexState =
    run.status !== "completed"
      ? { kind: "not_stored" }
      : {
          kind: "stored",
          indexed: finiteNumber(stats.links_emitted) ?? 0,
          omittedEmpty: finiteNumber(stats.pages_empty_content) ?? 0,
        };

  return {
    discoverySource,
    urlsDiscovered,
    selection: selectionState(stats, urlsDiscovered, urlsSelected, seedFetched),
    fetch: { seedFetched, frontierFetched, failed, notAttempted, bytesFetched, capHit },
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
