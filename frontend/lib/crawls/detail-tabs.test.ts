import { describe, expect, it } from "vitest";

import { DEFAULT_DETAIL_TAB, DETAIL_TABS, isDetailTab } from "./detail-tabs";

// `DETAIL_TABS` is read by two very different callers — `website-detail.tsx`'s `<Tabs>` and
// `github-callback.ts`'s redirect builder — and disagreeing about its order or its members is
// how one of them silently breaks. Pinning the exact array, not just its length or its
// membership, is what makes a reordering (accidental or "tidying") show up here first.

describe("DETAIL_TABS", () => {
  it("is exactly runs, output, schedule, publish, trends, in that order", () => {
    expect(DETAIL_TABS).toEqual(["runs", "output", "schedule", "publish", "trends"]);
  });

  it("DEFAULT_DETAIL_TAB is runs, and is one of DETAIL_TABS", () => {
    expect(DEFAULT_DETAIL_TAB).toBe("runs");
    expect(DETAIL_TABS).toContain(DEFAULT_DETAIL_TAB);
  });
});

describe("isDetailTab", () => {
  it.each(DETAIL_TABS)("accepts %s", (tab) => {
    expect(isDetailTab(tab)).toBe(true);
  });

  // Everything below is what `?tab=` looks like from someone who did not read this file:
  // absent, wrong case, a near-miss, or an attempt to smuggle something else into
  // `<Tabs value=…>`. Each has to fall back to `DEFAULT_DETAIL_TAB` rather than being trusted.
  it.each([null, "", "Publish", "publishing", "runs,output", "../../etc/passwd", "constructor", "toString"])(
    "rejects %s",
    (value) => {
      expect(isDetailTab(value)).toBe(false);
    }
  );
});
