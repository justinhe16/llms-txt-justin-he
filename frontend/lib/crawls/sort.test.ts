import { describe, expect, it } from "vitest";

import type { WebsiteListItem } from "../api/websites";
import { DEFAULT_SORT, ariaSortFor, nextSortState, sortWebsites } from "./sort";

// The table's ordering. Two properties here are load-bearing rather than cosmetic and are
// what most of this file is about: the sort must be TOTAL (rows that tie never swap between
// renders, or the table visibly flickers on every 3-second poll tick) and it must not mutate
// its argument (the array it is handed is React Query's cached data).

type Site = Pick<WebsiteListItem, "origin" | "latest_run">;

function site(origin: string, latestRun?: Partial<NonNullable<WebsiteListItem["latest_run"]>>) {
  return {
    origin,
    latest_run: latestRun
      ? ({ status: "completed", completed_at: null, started_at: null, ...latestRun } as never)
      : null,
  } as Site as WebsiteListItem;
}

const origins = (sites: WebsiteListItem[]) => sites.map((s) => s.origin);

describe("sortWebsites", () => {
  it("does not mutate the array it is given", () => {
    const input = [site("https://b.test"), site("https://a.test")];
    const before = origins(input);

    sortWebsites(input, { key: "site", direction: "asc" });

    expect(origins(input)).toEqual(before);
  });

  it("orders by origin in both directions", () => {
    const sites = [site("https://c.test"), site("https://a.test"), site("https://b.test")];

    expect(origins(sortWebsites(sites, { key: "site", direction: "asc" }))).toEqual([
      "https://a.test",
      "https://b.test",
      "https://c.test",
    ]);
    expect(origins(sortWebsites(sites, { key: "site", direction: "desc" }))).toEqual([
      "https://c.test",
      "https://b.test",
      "https://a.test",
    ]);
  });

  it("orders by last run, newest first, on the default sort", () => {
    const sites = [
      site("https://old.test", { completed_at: "2026-01-01T00:00:00Z" }),
      site("https://new.test", { completed_at: "2026-06-01T00:00:00Z" }),
    ];

    expect(origins(sortWebsites(sites, DEFAULT_SORT))).toEqual([
      "https://new.test",
      "https://old.test",
    ]);
  });

  it("keeps never-run sites at the bottom in BOTH directions", () => {
    // The one asymmetry in the module, and deliberate: "oldest last run first" and "sites
    // that have never run" are different questions, and answering the second at the top of
    // the first's results buries the row the reader asked for.
    const sites = [
      site("https://never.test"),
      site("https://ran.test", { completed_at: "2026-01-01T00:00:00Z" }),
    ];

    expect(origins(sortWebsites(sites, { key: "lastRun", direction: "desc" }))).toEqual([
      "https://ran.test",
      "https://never.test",
    ]);
    expect(origins(sortWebsites(sites, { key: "lastRun", direction: "asc" }))).toEqual([
      "https://ran.test",
      "https://never.test",
    ]);
  });

  it("breaks every tie by origin, so the order is total and stable", () => {
    const at = "2026-01-01T00:00:00Z";
    const sites = [
      site("https://c.test", { completed_at: at }),
      site("https://a.test", { completed_at: at }),
      site("https://b.test", { completed_at: at }),
    ];

    const once = origins(sortWebsites(sites, DEFAULT_SORT));
    const twice = origins(sortWebsites([...sites].reverse(), DEFAULT_SORT));

    expect(once).toEqual(["https://a.test", "https://b.test", "https://c.test"]);
    expect(twice).toEqual(once);
  });

  it("falls back to started_at when a run has not completed", () => {
    const sites = [
      site("https://finished.test", { completed_at: "2026-01-01T00:00:00Z" }),
      site("https://running.test", {
        status: "processing",
        started_at: "2026-06-01T00:00:00Z",
      }),
    ];

    expect(origins(sortWebsites(sites, DEFAULT_SORT))[0]).toBe("https://running.test");
  });
});

describe("nextSortState", () => {
  it("flips direction when the same column is clicked again", () => {
    const first = nextSortState({ key: "site", direction: "asc" }, "site");

    expect(first.direction).toBe("desc");
    expect(nextSortState(first, "site").direction).toBe("asc");
  });

  it("switches column rather than flipping when a different one is clicked", () => {
    const next = nextSortState({ key: "site", direction: "desc" }, "status");

    expect(next.key).toBe("status");
  });
});

describe("ariaSortFor", () => {
  it("reports a direction only for the sorted column", () => {
    const state = { key: "site", direction: "asc" } as const;

    expect(ariaSortFor(state, "site")).toBe("ascending");
    expect(ariaSortFor(state, "status")).toBe("none");
    expect(ariaSortFor({ key: "site", direction: "desc" }, "site")).toBe("descending");
  });
});
