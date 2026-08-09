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
  links_emitted: 6,
  pages_empty_content: 1,
  pages_http_error: 2,
  pages_off_origin: 1,
  urls_discovered: 40,
  urls_selected: 9,
  pages_failed: 0,
  pages_blocked: 0,
  blocked_reason: null,
  discovery_source: "sitemap",
};

describe("runProvenance — the Index stage", () => {
  it("reports each exclusion under its own reason", () => {
    const provenance = runProvenance(run(BASE));

    expect(provenance?.index).toEqual({
      kind: "stored",
      indexed: 6,
      omittedEmpty: 1,
      omittedHttpError: 2,
      omittedOffOrigin: 1,
    });
  });

  it("accounts for every fetched page, so the funnel adds up", () => {
    const provenance = runProvenance(run(BASE));
    const index = provenance?.index;

    expect(index?.kind).toBe("stored");
    if (index?.kind !== "stored") return;

    const explained =
      index.indexed + index.omittedEmpty + index.omittedHttpError + index.omittedOffOrigin;
    expect(explained).toBe(BASE.pages_crawled);
  });

  it("does not attribute the new exclusions to empty content", () => {
    // The regression, stated as its own test: a run that dropped two rate-limited pages and
    // one cross-origin redirect must not claim three pages had no extractable content.
    const index = runProvenance(run(BASE))?.index;

    expect(index?.kind === "stored" && index.omittedEmpty).toBe(1);
  });

  it("reads a pre-version-12 row as having no such exclusions", () => {
    // Rows written before version 12 carry neither key. `0` is the right reading: those runs
    // collected such pages into the index instead of excluding them, so there is no exclusion
    // to report — and reporting `null` would make the stage unrenderable for old runs.
    const { pages_http_error, pages_off_origin, ...v11 } = BASE;
    void pages_http_error;
    void pages_off_origin;

    const index = runProvenance(run({ ...v11, links_emitted: 9 }))?.index;

    expect(index).toEqual({
      kind: "stored",
      indexed: 9,
      omittedEmpty: 1,
      omittedHttpError: 0,
      omittedOffOrigin: 0,
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
