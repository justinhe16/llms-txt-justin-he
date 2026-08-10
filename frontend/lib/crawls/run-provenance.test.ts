import { describe, expect, it } from "vitest";

import type { RunDetail } from "../api/runs";
import { runProvenance } from "./run-provenance";

// The funnel's arithmetic. The property worth defending here is that every fetched page is
// accounted for by a NAMED reason: the panel draws `pagesCrawled - indexed` as a set of
// segments, and a reason it cannot name gets silently folded into whichever one it can.
//
// That has already gone wrong twice. Before `RUN_STATS_VERSION` 11 a Cloudflare challenge
// page was reported to the user as "no extractable content"; before version 12 a 404 and a
// cross-origin redirect were reported the same way. Both were false statements about the
// user's site rather than merely imprecise ones, which is why the invariant is asserted
// directly rather than left implied by the individual counts.

function run(stats: Record<string, unknown>, status = "completed"): Pick<
  RunDetail,
  "status" | "stats"
> {
  return { status, stats } as Pick<RunDetail, "status" | "stats">;
}

const BASE = {
  pages_crawled: 10,
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
      omittedHttpError: 1,
      omittedOffOrigin: 1,
      listedOptional: 2,
      omittedDuplicate: 2,
    });
  });

  it("accounts for every fetched page across all six named reasons, so the funnel adds up", () => {
    const provenance = runProvenance(run(BASE));
    const index = provenance?.index;

    expect(index?.kind).toBe("stored");
    if (index?.kind !== "stored") return;

    const explained =
      index.indexed +
      (index.listedOptional ?? 0) +
      index.omittedEmpty +
      index.omittedHttpError +
      index.omittedOffOrigin +
      (index.omittedDuplicate ?? 0);
    expect(explained).toBe(BASE.pages_crawled);
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
      omittedHttpError: 1,
      omittedOffOrigin: 1,
      listedOptional: null,
      omittedDuplicate: null,
    });
    // The four-term invariant a pre-version-13 row still closes under — the six-term one above
    // is simply not evaluable here, since two of its terms are `null`.
    expect(index?.kind === "stored" && index.indexed + index.omittedEmpty + index.omittedHttpError + index.omittedOffOrigin).toBe(
      BASE.pages_crawled
    );
  });

  it("reads a pre-version-12 row as having no http-error/off-origin exclusions either", () => {
    // Rows written before version 12 carry neither key. `0` is the right reading: those runs
    // collected such pages into the index instead of excluding them, so there is no exclusion
    // to report — and reporting `null` would make the stage unrenderable for old runs.
    const { pages_http_error, pages_off_origin, links_optional, links_duplicate, ...v11 } = BASE;
    void pages_http_error;
    void pages_off_origin;
    void links_optional;
    void links_duplicate;

    const index = runProvenance(run({ ...v11, links_emitted: 9 }))?.index;

    expect(index).toEqual({
      kind: "stored",
      indexed: 9,
      omittedEmpty: 1,
      omittedHttpError: 0,
      omittedOffOrigin: 0,
      listedOptional: null,
      omittedDuplicate: null,
    });
  });

  it("reports no index at all for a run that never completed", () => {
    // The gate is the run's status, not "does links_emitted exist" — a failed run can carry
    // partial stats, and rendering an index of zero for it would claim something false.
    expect(runProvenance(run(BASE, "failed"))?.index).toEqual({ kind: "not_stored" });
    expect(runProvenance(run(BASE, "running"))?.index).toEqual({ kind: "not_stored" });
  });
});

describe("runProvenance — guards", () => {
  it("returns null when a run has no stats to draw from", () => {
    expect(runProvenance({ status: "completed", stats: null } as never)).toBeNull();
  });

  it("treats a non-numeric stat as absent rather than rendering NaN", () => {
    const index = runProvenance(run({ ...BASE, pages_http_error: "two" }))?.index;

    expect(index?.kind === "stored" && index.omittedHttpError).toBe(0);
  });
});
