import { describe, expect, it } from "vitest";

import type { RunDetail } from "../api/runs";
import { CAP_HIT, fetchCapNote } from "./provenance-copy";
import { runProvenance } from "./run-provenance";

// The funnel's arithmetic. The property worth defending here is that every page and every
// selected URL is accounted for by a NAMED reason: both stages draw a total minus a set of
// segments, and a reason a stage cannot name gets silently folded into whichever one it can.
//
// That has already gone wrong three times. Before `RUN_STATS_VERSION` 11 a Cloudflare
// challenge page was reported to the user as "no extractable content"; before version 12 a
// 404 and a cross-origin redirect were reported the same way. Version 12 then fixed the Index
// stage and left the FETCH stage's residual still enumerating four outcomes where the crawler
// has six, so those same two pages came back as "Not attempted" — under a note reading "Every
// page this run selected was fetched." All three were false statements about the user's site
// rather than merely imprecise ones, which is why both invariants are asserted directly rather
// than left implied by the individual counts.
//
// `BASE` is therefore a row `internals/crawler.py` could actually emit, and that is a
// load-bearing property of this fixture rather than tidiness. A stats row whose numbers cannot
// co-occur can satisfy an invariant no real row satisfies: the version-12 fixture carried
// `pages_crawled: 10` alongside a non-2xx and a cross-origin page, which the crawler counts in
// place of `pages.append` — a real run with those counters reports 8. The six-term index sum
// closed on paper and could not close on any run. Every count below reconciles both ways:
//   index: links_emitted 3 + links_optional 2 + pages_empty_content 1 + links_duplicate 2 = 8
//          = pages_crawled
//   fetch: (pages_crawled - 1) 7 + pages_failed 0 + pages_blocked 0 + pages_http_error 1
//          + pages_off_origin 1 = 9 = urls_selected, so notAttempted is 0

function run(stats: Record<string, unknown>, status = "completed"): Pick<
  RunDetail,
  "status" | "stats"
> {
  return { status, stats } as Pick<RunDetail, "status" | "stats">;
}

const BASE = {
  pages_crawled: 8,
  links_emitted: 3,
  links_optional: 2,
  links_duplicate: 2,
  pages_empty_content: 1,
  pages_http_error: 1,
  pages_off_origin: 1,
  urls_discovered: 40,
  urls_selected: 9,
  pages_failed: 0,
  pages_blocked: 0,
  blocked_reason: null,
  discovery_source: "sitemap",
};

describe("runProvenance — the Index stage", () => {
  it("reports each exclusion under its own reason, plus the Optional bucket", () => {
    const provenance = runProvenance(run(BASE));

    expect(provenance?.index).toEqual({
      kind: "stored",
      indexed: 3,
      omittedEmpty: 1,
      listedOptional: 2,
      omittedDuplicate: 2,
    });
  });

  it("accounts for every fetched page across all four named reasons, so the funnel adds up", () => {
    const provenance = runProvenance(run(BASE));
    const index = provenance?.index;

    expect(index?.kind).toBe("stored");
    if (index?.kind !== "stored") return;

    const explained =
      index.indexed +
      (index.listedOptional ?? 0) +
      index.omittedEmpty +
      (index.omittedDuplicate ?? 0);
    expect(explained).toBe(BASE.pages_crawled);
  });

  it("leaves the non-2xx and cross-origin counts out of the index entirely", () => {
    // They are not index omissions, however much they read like them: `internals/crawler.py`
    // counts each in place of `pages.append`, so neither page is in `pages_crawled` and
    // including them overshot the stage's own total by exactly their count — while the same
    // two pages were drawn a second time in the Fetch stage's residual. `run_stats.py` states
    // it directly: "excluded from `pages_crawled` as well as from the index."
    const index = runProvenance(run(BASE))?.index;

    expect(index).not.toHaveProperty("omittedHttpError");
    expect(index).not.toHaveProperty("omittedOffOrigin");
  });

  it("does not attribute the new exclusions to empty content", () => {
    // The regression, stated as its own test: a run that dropped a rate-limited page, a
    // cross-origin redirect, and a duplicate must not claim three pages had no extractable
    // content.
    const index = runProvenance(run(BASE))?.index;

    expect(index?.kind === "stored" && index.omittedEmpty).toBe(1);
  });

  it("reads a pre-version-13 row as having no Optional bucket at all — null, never zero", () => {
    // Rows written before version 13 carry neither key. `null` is the right reading, NOT `0`:
    // those runs had no Optional concept and no dedup pass to report on, so "how many pages
    // were listed under Optional" and "how many were deduped" have no answer to give — reading
    // either as `0` would silently claim a fact about a run that could not have recorded it.
    const { links_optional, links_duplicate, ...v12 } = BASE;
    void links_optional;
    void links_duplicate;

    const index = runProvenance(run({ ...v12, links_emitted: 7 }))?.index;

    expect(index).toEqual({
      kind: "stored",
      indexed: 7,
      omittedEmpty: 1,
      listedOptional: null,
      omittedDuplicate: null,
    });
    // The two-term invariant a pre-version-13 row still closes under — the four-term one above
    // is simply not evaluable here, since two of its terms are `null`.
    expect(index?.kind === "stored" && index.indexed + index.omittedEmpty).toBe(
      BASE.pages_crawled
    );
  });

  it("reads a pre-version-12 row as having no http-error/off-origin outcomes either", () => {
    // Rows written before version 12 carry neither key. `0` is the right reading: those runs
    // collected such pages rather than dropping them, so there is nothing to report — and
    // reporting `null` would make the Fetch bar unrenderable for old runs. The counts are read
    // off `fetch` now, not `index`; on such a row every selected URL that was not fetched is
    // genuinely unaccounted for, so the residual absorbs it, which is what it is for.
    const { pages_http_error, pages_off_origin, links_optional, links_duplicate, ...v11 } = BASE;
    void pages_http_error;
    void pages_off_origin;
    void links_optional;
    void links_duplicate;

    const provenance = runProvenance(run({ ...v11, links_emitted: 7 }));

    expect(provenance?.index).toEqual({
      kind: "stored",
      indexed: 7,
      omittedEmpty: 1,
      listedOptional: null,
      omittedDuplicate: null,
    });
    expect(provenance?.fetch.httpError).toBe(0);
    expect(provenance?.fetch.offOrigin).toBe(0);
    expect(provenance?.fetch.notAttempted).toBe(2);
  });

  it("reports no index at all for a run that never completed", () => {
    // The gate is the run's status, not "does links_emitted exist" — a failed run can carry
    // partial stats, and rendering an index of zero for it would claim something false.
    expect(runProvenance(run(BASE, "failed"))?.index).toEqual({ kind: "not_stored" });
    expect(runProvenance(run(BASE, "running"))?.index).toEqual({ kind: "not_stored" });
  });
});

describe("runProvenance — the Fetch stage", () => {
  it("accounts for every selected URL, so nothing unnamed lands in the residual", () => {
    const fetch = runProvenance(run(BASE))?.fetch;

    expect(fetch?.frontierFetched).toBe(7);
    expect(fetch?.httpError).toBe(1);
    expect(fetch?.offOrigin).toBe(1);
    expect(fetch?.notAttempted).toBe(0);
  });

  it("does not call an attempted URL 'not attempted' when it answered with a non-2xx", () => {
    // The regression, at the shape of the run that found it: a 100-page budget, 99 URLs
    // selected, 97 frontier pages fetched, and two that answered with a 404 or a cross-origin
    // redirect. `notAttempted` enumerated `failed` and `blocked` only, so those two landed in
    // the residual and the panel reported "Not attempted 2" for two URLs it had attempted.
    const fetch = runProvenance(
      run({
        ...BASE,
        pages_crawled: 98,
        urls_selected: 99,
        pages_http_error: 1,
        pages_off_origin: 1,
      })
    )?.fetch;

    expect(fetch?.notAttempted).toBe(0);
  });

  it("still counts a genuinely unreached URL, so the residual is not merely zeroed out", () => {
    // The other direction, and the reason the fix is a subtraction rather than a deletion: a
    // cap that ended the run early leaves selected URLs no task ever reached, and that number
    // is the one thing this row exists to show.
    const fetch = runProvenance(
      run({ ...BASE, urls_selected: 40, cap_hit: "wall_clock" })
    )?.fetch;

    expect(fetch?.notAttempted).toBe(31);
  });

  it("does not subtract a failed seed's own status from a total the seed was never in", () => {
    // `pages_http_error` counts a non-2xx SEED as well, and the seed is never a member of
    // `selected` — so on a run where the seed itself 404'd, subtracting the raw counter would
    // report one fewer unreached URL than there were. `seedFetched` is what separates them:
    // no seed page means the frontier never ran, so none of that count is frontier-attributable.
    const fetch = runProvenance(
      run({ ...BASE, pages_crawled: 0, pages_http_error: 1, pages_off_origin: 0 }, "failed")
    )?.fetch;

    expect(fetch?.seedFetched).toBe(false);
    expect(fetch?.httpError).toBe(0);
    expect(fetch?.notAttempted).toBe(9);
  });
});

describe("fetchCapNote", () => {
  const counts = { notAttempted: 0, failed: 0, blocked: 0, httpError: 0, offOrigin: 0 };

  it("does not claim every selected page was fetched beside a nonzero unreached count", () => {
    // The false sentence this pairs with the false number above: the guard read `failed`
    // alone, so any other way of leaving a URL unreached printed the reassurance anyway.
    const note = fetchCapNote(null, 254, { ...counts, notAttempted: 2 });

    expect(note).not.toContain("Every page this run selected was fetched");
    expect(note).toContain("254 ranked URLs did not fit into the frontier");
  });

  it("says 'attempted', not 'fetched', when some selected URL yielded no page", () => {
    const note = fetchCapNote(null, 254, { ...counts, httpError: 2 });

    expect(note).toContain("Every URL this run selected was attempted.");
  });

  it("keeps the stronger claim for a run that earned it", () => {
    expect(fetchCapNote(null, 254, counts)).toContain(
      "Every page this run selected was fetched."
    );
  });

  it("still defers to a cap that actually ended the fetch loop", () => {
    expect(fetchCapNote("bytes", 254, counts)).toBe(CAP_HIT.bytes);
  });
});

describe("runProvenance — guards", () => {
  it("returns null when a run has no stats to draw from", () => {
    expect(runProvenance({ status: "completed", stats: null } as never)).toBeNull();
  });

  it("treats a non-numeric stat as absent rather than rendering NaN", () => {
    const fetch = runProvenance(run({ ...BASE, pages_http_error: "two" }))?.fetch;

    expect(fetch?.httpError).toBe(0);
    expect(fetch?.notAttempted).toBe(1);
  });
});
