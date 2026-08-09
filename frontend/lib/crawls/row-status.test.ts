import { describe, expect, it } from "vitest";

import type { WebsiteListItem } from "../api/websites";
import {
  formatIntervalMinutes,
  lastActivityAt,
  pagesCrawled,
  rowIsActive,
  rowStatus,
  rowStatusFromRunStatus,
  rowStatusLabel,
  rowStatusToken,
} from "./row-status";

function site(latestRun?: Partial<NonNullable<WebsiteListItem["latest_run"]>>) {
  return {
    origin: "https://example.test",
    latest_run: latestRun
      ? ({ status: "completed", completed_at: null, started_at: null, ...latestRun } as never)
      : null,
  } as WebsiteListItem;
}

describe("rowStatus", () => {
  it("is never-run for a website with no runs", () => {
    expect(rowStatus(site())).toBe("never-run");
  });

  it.each([
    ["completed", "completed"],
    ["processing", "running"],
    ["pending", "queued"],
    ["failed", "failed"],
  ] as const)("maps a %s run to the %s row status", (runStatus, expected) => {
    expect(rowStatus(site({ status: runStatus }))).toBe(expected);
    expect(rowStatusFromRunStatus(runStatus)).toBe(expected);
  });
});

describe("rowIsActive", () => {
  it("is true only while a run is still moving", () => {
    expect(rowIsActive(site({ status: "processing" }))).toBe(true);
    expect(rowIsActive(site({ status: "pending" }))).toBe(true);
    expect(rowIsActive(site({ status: "completed" }))).toBe(false);
    expect(rowIsActive(site({ status: "failed" }))).toBe(false);
    expect(rowIsActive(site())).toBe(false);
  });
});

describe("rowStatusToken and rowStatusLabel", () => {
  it("has a token and a label for every status, including never-run", () => {
    // A `Record` missing a key is a `tsc` error in this module by design; this covers the
    // runtime half — that neither lookup returns undefined for any member of the union.
    const all = ["completed", "running", "queued", "failed", "never-run"] as const;

    for (const status of all) {
      expect(rowStatusToken(status)).toBeTruthy();
      expect(rowStatusLabel(status)).toBeTruthy();
    }
  });

  it("groups running and queued under one processing token", () => {
    expect(rowStatusToken("running")).toBe(rowStatusToken("queued"));
    expect(rowStatusToken("completed")).not.toBe(rowStatusToken("failed"));
  });
});

describe("lastActivityAt", () => {
  it("prefers completion, falls back to start, and is null with no run", () => {
    expect(lastActivityAt(site({ completed_at: "2026-01-01T00:00:00Z" }))).toBe(
      "2026-01-01T00:00:00Z"
    );
    expect(lastActivityAt(site({ started_at: "2026-02-01T00:00:00Z" }))).toBe(
      "2026-02-01T00:00:00Z"
    );
    expect(lastActivityAt(site())).toBeNull();
  });
});

describe("pagesCrawled", () => {
  it("preserves a real zero rather than collapsing it to null", () => {
    // A failed run that crawled nothing genuinely crawled 0 pages; rendering that as "—"
    // would lose the difference between "none" and "not known yet".
    expect(pagesCrawled(site({ pages_crawled: 0 } as never))).toBe(0);
    expect(pagesCrawled(site({ pages_crawled: 12 } as never))).toBe(12);
    expect(pagesCrawled(site())).toBeNull();
  });
});

describe("formatIntervalMinutes", () => {
  it.each([
    [60, "hourly"],
    [1440, "daily"],
    [10080, "weekly"],
  ])("names the three preset intervals (%i minutes)", (minutes, expected) => {
    expect(formatIntervalMinutes(minutes)).toBe(expected);
  });

  it("falls back to a compact duration for an interval with no name", () => {
    expect(formatIntervalMinutes(30)).toBe("30m");
    expect(formatIntervalMinutes(90)).toBe("90m");
    expect(formatIntervalMinutes(360)).toBe("6h");
    expect(formatIntervalMinutes(2880)).toBe("2d");
  });
});
