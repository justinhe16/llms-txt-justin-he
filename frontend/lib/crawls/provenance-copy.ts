// The strings the Output tab's provenance panel (components/crawls/crawl-provenance.tsx)
// needs to explain how a run got from its seed URL to its index — mirroring
// `lib/crawls/enrichment-copy.ts`: one module holding copy two things would otherwise have to
// keep in sync by hand.
//
// The two things here are `backend/app/features/crawl/internals/url_ranking.py`'s
// `_RULE_ORDER` (the twelve selection-rule keys, and the order they are checked in) and
// `app/docs/(run)/page.mdx`'s Selection section (the same twelve rules, in the same order, in
// prose for a human reader who never opens this panel). `backend/tests/test_url_ranking.py`'s
// `test_rule_order_is_pinned` is the gate on the first — it fails loudly the day someone adds
// a thirteenth rule to `_RULE_ORDER` without updating `SELECTION_RULE_ORDER` below to match.
// Nothing pins the second automatically; the two are matched by hand, deliberately, where
// they overlap (see `SELECTION_RULES`'s own comment for which rows that is).
//
// **Rule labels belong here, not in `url_ranking.py`.** The backend emits rule KEYS
// (`dated_archive`, `taxonomy`, ...) — bare, lowercase, snake_case identifiers, pinned by
// `test_every_rule_key_is_a_bare_snake_case_identifier`. The human phrasing, and the one-line
// explanation of what each rule actually does, is presentation, and presentation is this
// module's whole job.

/**
 * `internals/url_ranking.py`'s `_RULE_ORDER`, verbatim and in the same order — pre-filter
 * rules in the order they are checked, then the two during-selection rules.
 *
 * **This order is load-bearing, not cosmetic.** `runs.stats["dropped"]` is a `jsonb` column,
 * and Postgres does not preserve an object's key order on the way in — it re-orders by key
 * length, then bytewise. A renderer that iterates `Object.entries(stats.dropped)` therefore
 * renders rules in an arbitrary order that silently drifts from run to run. `lib/crawls/
 * run-provenance.ts`'s `runProvenance` drives its funnel rows from THIS constant instead,
 * checking each key's presence in the stored map rather than trusting the map's own iteration
 * order — see that module's own docstring for where the loop actually happens.
 */
export const SELECTION_RULE_ORDER = [
  "unparseable",
  "duplicate",
  "seed",
  "off_origin",
  "robots_disallowed",
  "dated_archive",
  "taxonomy",
  "pagination",
  "feed",
  "changelog",
  "localized_duplicate",
  "over_limit",
] as const;

export type SelectionRuleKey = (typeof SELECTION_RULE_ORDER)[number];

/**
 * One rule's copy: a short label for the table's row heading, and the one-line plain-language
 * explanation that — with no example URLs anywhere in this panel (a deliberate scope decision,
 * not a first cut) — does the entire job of making a rule concrete. A row reading "-412
 * dated_archive" with no gloss is a worse panel than no panel.
 *
 * Four rows (`robots_disallowed`, `dated_archive`, `taxonomy`, `localized_duplicate`) match
 * `/docs#selection`'s own wording where that page glosses the rule; that page's Selection
 * section is deliberately terse and does not gloss the other eight, so this table's copy for
 * those is a superset written directly from the rule's own implementation instead.
 */
export const SELECTION_RULES: Record<SelectionRuleKey, { label: string; explanation: string }> = {
  unparseable: {
    label: "Unparseable",
    explanation: "A URL that could not be read as an http or https address.",
  },
  duplicate: {
    label: "Duplicate",
    explanation: "The same URL after normalization — a repeat of one already counted.",
  },
  seed: {
    label: "Seed URL",
    explanation: "The run's own starting page, fetched first and never counted twice.",
  },
  off_origin: {
    label: "Off origin",
    explanation: "A URL on a different scheme, host or port from the seed.",
  },
  robots_disallowed: {
    label: "Disallowed by robots.txt",
    explanation:
      "The one operator-authored rule — every other rule guesses from a URL's shape; this one drops exactly what the site's own robots.txt asked not to be fetched.",
  },
  dated_archive: {
    label: "Dated archive",
    explanation: "A path segment holding a year from 1990 to 2099.",
  },
  taxonomy: {
    label: "Taxonomy",
    explanation: "A path containing a segment such as tag, category or author.",
  },
  pagination: {
    label: "Pagination",
    explanation: "A page query parameter, or a page path segment followed by a number.",
  },
  feed: {
    label: "Feed",
    explanation: "A path ending in feed, .rss, .atom or .xml.",
  },
  changelog: {
    label: "Changelog",
    explanation: "A path segment reading changelog, change-log or release-notes.",
  },
  localized_duplicate: {
    label: "Localized duplicate",
    explanation:
      "A page under a locale prefix whose unprefixed form ranked higher and was already selected.",
  },
  over_limit: {
    label: "Over the page limit",
    explanation:
      "The one row here that is not a judgement about the URL. These passed every rule above; the survivors are then scored, sorted, and the run's page budget takes the top of the list. Everything below the cut is dropped by position — there is no quality bar it failed.",
  },
};

/**
 * The degrade-to-key lookup the `[Labels]` acceptance criterion asks for directly: an
 * unrecognised key — a rule this panel shipped before the backend added it — renders the raw
 * key with no explanation, rather than a blank row or a thrown error. `selection-copy`'s own
 * `Record` above is exhaustive over today's `SelectionRuleKey`; this function is what stays
 * safe the day that stops being true.
 */
export function selectionRuleCopy(key: string): { label: string; explanation: string | null } {
  const known = (SELECTION_RULES as Record<string, { label: string; explanation: string }>)[key];
  return known ?? { label: key, explanation: null };
}

/**
 * Every `discovery_source` value the backend can emit — `internals/sitemap.py`'s four entry
 * points (`sitemap`, `sitemap_index`, `robots`, `none`) plus `service.py`'s `RunDiscoverySource`
 * fifth, `links`, the depth-1 seed-link fallback. Verified against both modules rather than
 * assumed.
 *
 * `links` names the ticket's own Discovery requirement directly: when the source is `links`,
 * the panel says the sitemap fallback was used and the seed page's own links were read
 * instead, rather than merely naming the source and leaving a reader to know what "links"
 * means. `none` is the "discovery found nothing" case — `run-provenance.ts` renders it as one
 * of two sentences, not an empty table, depending on whether the seed itself was fetched — see
 * `discoverySourceCopy` below: this `"none"` entry's `explanation` is not always the one that
 * ends up rendered.
 */
export const DISCOVERY_SOURCE: Record<string, { label: string; explanation: string }> = {
  sitemap: {
    label: "Sitemap",
    explanation: "Found at /sitemap.xml.",
  },
  sitemap_index: {
    label: "Sitemap index",
    explanation: "Found at /sitemap_index.xml, one level of indirection into the site's sitemaps.",
  },
  robots: {
    label: "robots.txt",
    explanation: "Found via a Sitemap: line in the site's robots.txt.",
  },
  links: {
    label: "Seed page links",
    explanation:
      "No sitemap produced anything, so the sitemap fallback was used and the seed page's own links were read instead.",
  },
  none: {
    label: "None",
    explanation: "No discovery source produced anything — the run crawled the seed alone.",
  },
};

/**
 * `DISCOVERY_SOURCE["none"]`'s explanation is only true when the seed itself was fetched — it
 * says outright that the run "crawled the seed alone," which is a real claim about a fetch
 * that happened, not merely about discovery finding nothing. When the seed never landed (a
 * domain that is entirely down, `fetch.seedFetched === false`), this replaces it with a
 * sentence that claims nothing about the seed beyond the fact that it was not fetched — the
 * Discovery-section half of the same distinction `run-provenance.ts`'s `"seed_not_fetched"`
 * `SelectionState` draws for the Selection section below it. The two must read as one story,
 * not two: see that type's own docstring for the row (`test_run_persistence.py`'s
 * `test_a_failed_seed_reports_no_discovery_source_even_with_the_fallback_armed`) that would
 * otherwise have this panel contradict itself between its first two sections.
 */
export const DISCOVERY_NONE_SEED_NOT_FETCHED =
  "No discovery source produced anything, and the seed page itself could not be fetched — nothing was crawled for this run.";

/**
 * `discoverySource` plus whether the seed itself landed, turned into the Discovery section's
 * label/explanation pair. A thin wrapper around `DISCOVERY_SOURCE`, not a second lookup table
 * — every entry is unchanged except `"none"`, which needs `fetch.seedFetched` to pick between
 * the two sentences above. `null` when `source` is `null` or unrecognised, matching
 * `DISCOVERY_SOURCE[source] ?? null`'s existing contract.
 */
export function discoverySourceCopy(
  source: string | null,
  seedFetched: boolean
): { label: string; explanation: string } | null {
  if (source === null) return null;
  const copy = DISCOVERY_SOURCE[source];
  if (copy === undefined) return null;
  if (source === "none" && !seedFetched) {
    return { label: copy.label, explanation: DISCOVERY_NONE_SEED_NOT_FETCHED };
  }
  return copy;
}

/**
 * Every `cap_hit` value `internals/crawler.py` can record (`pages`, `bytes`, `wall_clock`),
 * plus the sentence for `cap_hit: null`. Wording is bound by ARCHITECTURE.md §3.4: "hitting a
 * cap is a success, not a failure" — never "stopped short", "cut off", or a failure colour
 * anywhere in this panel's Fetch stage.
 */
export const CAP_HIT: Record<string, string> = {
  pages: "Ended on the page cap — the run fetched as many pages as its budget allows.",
  bytes: "Ended on the byte cap — the run's total response size reached its budget.",
  wall_clock: "Ended on the time cap — the run reached its wall-clock budget.",
};

/** `cap_hit: null` — every page the run selected was fetched before any cap was reached. */
export const NO_CAP_HIT = "No cap was hit — the run finished before any budget ran out.";

/**
 * `capHit` (`fetch.capHit`, `run-provenance.ts`) turned into the Fetch stage's closing
 * sentence. Not a bare `CAP_HIT[capHit] ?? NO_CAP_HIT` lookup, because that fallback asserts
 * the opposite of what happened for a `cap_hit` value this table has no copy for yet: it
 * would print "no cap was hit" onto a row that a cap DID end. Not reachable today — every
 * value `internals/crawler.py` can record is in `CAP_HIT` — but `selectionRuleCopy` already
 * sets the precedent for degrading to the raw key rather than asserting a fact with no
 * evidence for it, and this is that same precedent applied here. Still bound by
 * ARCHITECTURE.md §3.4: even the degrade path stays out of failure language.
 */
export function capHitCopy(capHit: string | null): string {
  if (capHit === null) return NO_CAP_HIT;
  return CAP_HIT[capHit] ?? `Ended on a cap this panel has no name for yet: "${capHit}".`;
}

/**
 * The Fetch stage's closing sentence, with the one case `capHitCopy` alone gets wrong.
 *
 * **`NO_CAP_HIT` is false on any run whose page budget was spent at selection, and on a site
 * larger than the budget it is false every single time.** `cap_hit` answers "which cap stopped
 * the fetch loop," and the page budget stops a run one stage earlier — `select_urls` truncates
 * the frontier to `max_pages - 1` before `crawl_site` is handed it, so neither of the crawl
 * loop's two `cap_hit: "pages"` sites can fire (`lib/crawls/run-provenance.ts`'s "The cap the
 * Fetch stage cannot see" section walks both). Printing "no cap was hit — the run finished
 * before any budget ran out" directly under a Selection table reading "-252 Over the page
 * limit" is not a rough edge; it is the panel stating the opposite of what happened, on
 * exactly the runs a reader opened it to understand.
 *
 * `overLimit > 0` is the substitute signal, and it is a sound one in both directions: the rule
 * fires if and only if ranking had more candidates than budget. `null` (a row with no per-rule
 * split) falls through to `capHitCopy` unchanged rather than guessing — a version-9-or-earlier
 * row genuinely does not record which rule dropped what.
 *
 * `failed` is taken as well, because a budget that was spent and a page that failed are
 * independent facts and the sentence has to be true of both — see the comment on the branch
 * itself.
 *
 * Still bound by ARCHITECTURE.md §3.4: the budget being spent is a SUCCESS. "Reached", never
 * "cut off", "stopped short" or "hit its limit" — and the sentence leads with the pages that
 * were fetched rather than the ones that were not.
 */
export function fetchCapNote(
  capHit: string | null,
  overLimit: number | null,
  failed: number
): string {
  if (capHit === null && overLimit !== null && overLimit > 0) {
    const budget = `This run's page budget was reached at selection rather than here — ${overLimit.toLocaleString()} ranked ${overLimit === 1 ? "URL" : "URLs"} did not fit into the frontier, so the fetch loop never had a cap left to reach.`;
    // `failed` is why this is not one sentence. `pages_failed` is incremented by
    // `fetch_frontier_url`'s own `except Exception` (`internals/crawler.py`) and has nothing
    // to do with any cap — a frontier URL can fail on a network error, a non-2xx, or an SSRF
    // refusal on a run that tripped nothing. And on a `cap_hit: null` run it is the ONLY way a
    // selected URL goes unfetched: no task short-circuited on the `cap_hit is not None` guard,
    // so every one of them was attempted and `frontierFetched + failed === urlsSelected`
    // exactly. Leading with "every page this run selected was fetched" there would put a false
    // claim directly beside the `Failed` legend entry counting the ones that were not — the
    // same shape of contradiction this whole function exists to remove, reintroduced one state
    // over.
    return failed > 0 ? budget : `Every page this run selected was fetched. ${budget}`;
  }
  return capHitCopy(capHit);
}

/**
 * The Selection stage's four non-`breakdown` sentences, in one place rather than inlined in
 * the renderer's `switch` — the same reason every other string in this module is here.
 *
 * `totalsOnly` is the one worth reading twice. A run whose `stats` carry no `dropped` key is
 * NOT a run we know nothing about: `urls_discovered` and `urls_selected` are version-4 keys, so
 * both ends of the funnel are still on the row and the total dropped is the difference between
 * them (`lib/crawls/run-provenance.ts`'s `"totals_only"` state). What is missing is only which
 * rules fired. Saying that precisely — rather than "the breakdown isn't available" — is the
 * difference between a panel that reports a gap in the record and one that looks broken, and
 * every run crawled before PER-196 deployed is in exactly this state.
 *
 * `unavailable` is what is left when even the totals are absent: a version-3-or-earlier row,
 * from before discovery counts were recorded at all.
 */
export const SELECTION_STATE_COPY = {
  unavailable:
    "This run predates the selection counters, so nothing about how its frontier was chosen was recorded.",
  totalsOnly:
    "This run was crawled before the per-rule breakdown was recorded. The totals survived; which rules did the dropping did not.",
  seedNotFetched: "Nothing to select — discovery found no candidate URLs.",
  seedOnly:
    "Nothing to select — discovery found no candidate URLs, so the seed page was the whole crawl.",
  nothingDropped: "No rule dropped anything — every URL discovery found was selected.",
} as const;

/**
 * The Selection stage's budget sentence — the panel's answer to "why did ranking drop those,
 * and could more have got through?"
 *
 * Written as a function rather than a constant because both halves of the answer are numbers:
 * how many candidates were actually ranked, and how many of them the budget had room for.
 *
 * **Two readings this replaces.** "99 selected, 252 dropped" invites the first — that the 252
 * failed something — and `over_limit`'s own row copy above answers that one. The second is
 * subtler and is what this sentence exists for: that selecting more was *possible*. It was
 * not, and not by a policy that could go either way — `select_urls` is called with
 * `limit = max_pages - 1` (`CrawlService.execute_run`), so `frontierLimit` is not a number
 * ranking aimed at but the number it could not exceed. A reader who has just seen 252 URLs
 * dropped is entitled to know whether a better-ranked site would have got 150 through. It
 * would not.
 *
 * @param eligible How many candidates reached the budget check — `selected + overLimit`. Every
 *   one of them passed all eleven preceding rules; that is what makes "passed every rule"
 *   below a true statement about exactly this set rather than about the discovered total.
 * @param budget This run's `PageBudget` (`lib/crawls/run-provenance.ts`).
 */
export function selectionBudgetNote(
  eligible: number,
  budget: { maxPages: number; frontierLimit: number }
): string {
  return (
    `${eligible.toLocaleString()} URLs passed every rule above and were ranked. ` +
    `The budget takes the top ${budget.frontierLimit.toLocaleString()}, so the rest were dropped on position alone. ` +
    `No run selects more: the budget is ${budget.maxPages.toLocaleString()} pages and the seed is one of them.`
  );
}

/**
 * The Selection stage's sentence when the budget did NOT bind — every ranked candidate fit.
 *
 * The counterpart to `selectionBudgetNote`, and worth stating rather than leaving the stage
 * silent: it tells a reader that the numbers above are the whole site, not the top of it, which
 * is the single most useful thing this stage can say about a run where ranking never had to
 * choose. Only reachable with a recorded budget — the derivation `PageBudget` falls back to on
 * an old row is available exactly when the budget DID bind.
 */
export function selectionBudgetSpareNote(budget: { maxPages: number }): string {
  return `Every ranked URL fit — the budget is ${budget.maxPages.toLocaleString()} pages and the site did not fill it, so nothing was dropped for want of room.`;
}

/**
 * The unit that follows each stage's headline number — "2 URLs found", "1 page listed".
 *
 * Both forms, because a run that lists one page is common enough that "1 pages listed" would
 * be on screen daily. `stageUnit` below picks between them; nothing renders these directly.
 *
 * Lower-case and plural-by-default, because every one of them is read as the tail of a number
 * and never as a label on its own. `fetch` counts PAGES rather than URLs on purpose: it is the
 * first stage where the seed — never a member of `selected` (`lib/crawls/run-provenance.ts`'s
 * own invariants section) — is part of the count, so a reader who notices the count going UP
 * between Selection and Fetch has the noun change as the first hint of why, before they reach
 * the segment labelled "Seed page" that says it outright.
 */
export const PROVENANCE_STAGE_UNITS = {
  discovery: { one: "URL found", other: "URLs found" },
  selection: { one: "URL selected", other: "URLs selected" },
  fetch: { one: "page fetched", other: "pages fetched" },
  index: { one: "page listed", other: "pages listed" },
} as const;

/** One stage's unit, agreeing with its headline count. An unrecorded count (`null`, rendered
 * as an em dash) takes the plural — there is no number for it to agree with, and "— pages
 * listed" reads as the general case the dash stands in for. */
export function stageUnit(unit: { one: string; other: string }, count: number | null): string {
  return count === 1 ? unit.one : unit.other;
}

/**
 * The label on each segment of a stage's funnel bar, and in the legend under it.
 *
 * Two rules hold across all of them, both inherited from ARCHITECTURE.md §3.4's "hitting a cap
 * is a success": nothing here is failure language for an outcome that is not a failure
 * (`notAttempted` is "Not attempted", never "skipped" or "missed"), and every segment names a
 * fact the row actually recorded rather than one derived from a subtraction the panel hopes is
 * right.
 *
 * `blocked` (`RUN_STATS_VERSION` 11) follows the same rule as `notAttempted`: a page a WAF or
 * CDN challenged is not a failure of this crawler's own doing, so the label names what
 * happened rather than passing judgement — `crawl-provenance.tsx`'s `SEGMENT_FILL` colours it
 * the same neutral grey `dropped`/`notAttempted`/`omittedEmpty` already use, never the rose
 * `failed` gets.
 */
export const PROVENANCE_SEGMENTS = {
  discovered: "Discovered",
  selected: "Selected",
  dropped: "Dropped",
  seed: "Seed page",
  frontier: "From the frontier",
  failed: "Failed",
  blocked: "Blocked",
  notAttempted: "Not attempted",
  listed: "Listed in llms.txt",
  omittedEmpty: "Omitted as empty",
  omittedHttpError: "Omitted: not a page",
  omittedOffOrigin: "Omitted: another site",
  // `RUN_STATS_VERSION` 13 (the curated-index ticket): a page grouped under `## Optional` is
  // carried forward into the artifact, not lost — it names a FACT ("this page is here, just
  // not in the main body"), never failure language, the same rule every segment on this list
  // already follows.
  listedOptional: "Listed under Optional",
  omittedDuplicate: "Omitted: duplicate",
} as const;

/**
 * `blocked_reason` (`internals/blocked.py`'s `BlockReason`) turned into a label and a
 * plain-language explanation — the Fetch stage's own copy for the one new segment this
 * ticket adds. Deliberately says what this crawler did NOT do (solve the challenge) as much
 * as what the site did, mirroring `_BLOCKED_MESSAGES` on the backend
 * (`app.features.crawl.service`) — a signed-in user reading either copy should come away with
 * "this crawler detected and stopped," never "this crawler tried and failed."
 */
export const BLOCKED_REASON: Record<string, { label: string; explanation: string }> = {
  challenge: {
    label: "Challenge",
    explanation:
      "This page returned an automated-traffic challenge (for example, Cloudflare's managed challenge) that this crawler does not attempt to solve.",
  },
  denied: {
    label: "Denied",
    explanation:
      "This page's server denied this crawler's request outright (401 or 403), with no challenge to solve.",
  },
};

/**
 * The degrade-to-key lookup `selectionRuleCopy` already sets the precedent for, applied to
 * `BLOCKED_REASON`: a `blocked_reason` value this panel has no copy for yet renders the raw
 * key with an explanation naming it directly, rather than a blank row or a thrown error.
 */
export function blockedReasonCopy(reason: string): { label: string; explanation: string } {
  return (
    BLOCKED_REASON[reason] ?? {
      label: reason,
      explanation: `This page was blocked ("${reason}").`,
    }
  );
}

/** The Fetch stage's byte total, labelled — the one number in this panel that is not a count of
 * URLs or pages, which is why it sits beside the funnel bar rather than in it. */
export const PROVENANCE_BYTES_LABEL = "Downloaded";

/** The Selection stage's page budget, labelled — deliberately the same
 * `label + value` shape as `PROVENANCE_BYTES_LABEL` above, and rendered in the same slot, so
 * the two stages that have a configured number behind them present it identically. "Page
 * budget", not "Page limit": the value is what the run was ALLOWED, and every other number in
 * this panel is what it did. */
export const PROVENANCE_BUDGET_LABEL = "Page budget";

/** The page budget's own unit, for the `PROVENANCE_BUDGET_LABEL` value — "100 pages", matching
 * the Fetch stage's noun rather than Selection's own "URLs", because the budget counts what is
 * FETCHED (the seed included) and not what is selected. The note underneath is what reconciles
 * the two. */
export const PROVENANCE_BUDGET_UNIT = { one: "page", other: "pages" } as const;

/** The `<summary>` line's right-hand micro-summary, so a reader gets the shape of the run
 * without opening the panel. Rendered as "2 found · 3 fetched · 1 listed", omitting any part
 * this run did not record — `blocked` joins the line only when `fetch.blocked > 0`, the same
 * "omit rather than show a zero" rule the other three parts already follow. */
export const PROVENANCE_PREVIEW = {
  found: "found",
  fetched: "fetched",
  blocked: "blocked",
  listed: "listed",
} as const;

/** The disclosure's own summary text, closed by default. */
export const PROVENANCE_SUMMARY = "Show how this was built";

/** The four stage headings, in render order. */
export const PROVENANCE_HEADINGS = {
  discovery: "Discovery",
  selection: "Selection",
  fetch: "Fetch",
  index: "Index",
} as const;

/** The Selection heading's link text and target — the full ruleset lives there, not restated
 * here row by row beyond the label/explanation pairs above. */
export const SELECTION_DOCS_LINK = { href: "/docs#selection", text: "Selection" };
