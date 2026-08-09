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
    explanation: "Ranked below the cut — the run's page budget was already full.",
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
