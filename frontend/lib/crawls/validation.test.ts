import { describe, expect, it } from "vitest";

import type { RunDetail } from "../api/runs";
import { runValidation, validationLabel } from "./validation";

// The property worth defending here is that `conforms` and "no findings" never collapse into
// one another. llmstxt.org requires nothing but an H1, so an artifact can satisfy the spec and
// still be a poor index — and a UI that rendered one green tick for both would hide the only
// half a user can act on. Every test below that asserts a verdict also asserts the counts
// behind it, so "valid" can never quietly come to mean "valid and good".
//
// The second property is that a missing report is never read as a passing one. Every run
// crawled before this check shipped has no `validation` key, and `runValidation` has to return
// `null` for that rather than a default-constructed clean report.

function run(stats: Record<string, unknown> | null): RunDetail {
  return { stats } as RunDetail;
}

const CLEAN = {
  conforms: true,
  error_count: 0,
  warning_count: 0,
  findings: [],
  findings_truncated: false,
  structure: { h1: "Acme", has_summary: true, section_count: 2, link_count: 9 },
  version: 1,
};

describe("runValidation — the three verdicts", () => {
  it("reads a clean report as clean", () => {
    const validation = runValidation(run({ validation: CLEAN }));

    expect(validation).toEqual({
      verdict: "clean",
      errorCount: 0,
      warningCount: 0,
      findings: [],
      truncated: false,
    });
  });

  it("reads a conformant report with warnings as `warnings`, not as clean and not as errors", () => {
    const validation = runValidation(
      run({
        validation: {
          ...CLEAN,
          warning_count: 2,
          findings: [
            { code: "no_summary", severity: "warning", line: null, message: "No summary." },
            { code: "empty_section", severity: "warning", line: 12, message: "Empty section." },
          ],
        },
      })
    );

    // The spec IS satisfied — this is the pairing the whole module exists to keep visible.
    expect(validation?.verdict).toBe("warnings");
    expect(validation?.errorCount).toBe(0);
    expect(validation?.warningCount).toBe(2);
    expect(validation?.findings).toHaveLength(2);
  });

  it("reads a non-conformant report as `errors`", () => {
    const validation = runValidation(
      run({
        validation: {
          ...CLEAN,
          conforms: false,
          error_count: 1,
          findings: [{ code: "missing_h1", severity: "error", line: null, message: "No H1." }],
        },
      })
    );

    expect(validation?.verdict).toBe("errors");
    expect(validation?.errorCount).toBe(1);
  });

  it("trusts `conforms` over the counts when the two disagree", () => {
    // `conforms` is the backend's own verdict; the counts summarize it. A row where they
    // disagree is a bug upstream, and the verdict is the field that decided whether the
    // document violates the spec — so it is the one that wins here.
    const validation = runValidation(
      run({ validation: { ...CLEAN, conforms: false, error_count: 0, warning_count: 0 } })
    );

    expect(validation?.verdict).toBe("errors");
  });
});

describe("runValidation — a missing report is never a passing one", () => {
  it("returns null for a run with no `validation` key", () => {
    expect(runValidation(run({ pages_crawled: 4 }))).toBeNull();
  });

  it("returns null for an explicit `validation: null` — a run that produced no index", () => {
    expect(runValidation(run({ validation: null }))).toBeNull();
  });

  it("returns null for a run with no stats at all", () => {
    expect(runValidation(run(null))).toBeNull();
  });

  it("returns null when `validation` is not an object", () => {
    for (const value of ["yes", 1, true, []]) {
      expect(runValidation(run({ validation: value }))).toBeNull();
    }
  });

  it("returns null when `conforms` is missing or not a boolean", () => {
    expect(runValidation(run({ validation: { error_count: 0 } }))).toBeNull();
    expect(runValidation(run({ validation: { ...CLEAN, conforms: "true" } }))).toBeNull();
  });
});

describe("runValidation — defensive reads of jsonb", () => {
  it("drops a finding with no renderable message or severity, keeping the backend's counts", () => {
    const validation = runValidation(
      run({
        validation: {
          ...CLEAN,
          warning_count: 3,
          findings: [
            { code: "no_summary", severity: "warning", line: null, message: "No summary." },
            { code: "broken", severity: "warning", line: 2 }, // no message
            { code: "broken", severity: "critical", line: 3, message: "Unknown severity." },
            "not an object",
            null,
          ],
        },
      })
    );

    expect(validation?.findings).toHaveLength(1);
    // Unchanged by the dropped entries: the counts are the backend's totals, not a length.
    expect(validation?.warningCount).toBe(3);
  });

  it("defaults a non-numeric or negative count to zero rather than rendering NaN", () => {
    const validation = runValidation(
      run({ validation: { ...CLEAN, error_count: "lots", warning_count: -4 } })
    );

    expect(validation?.errorCount).toBe(0);
    expect(validation?.warningCount).toBe(0);
  });

  it("keeps a finding whose `code` or `line` is unusable, since the message is what renders", () => {
    const validation = runValidation(
      run({
        validation: {
          ...CLEAN,
          findings: [{ severity: "warning", line: "seven", message: "Still worth showing." }],
        },
      })
    );

    expect(validation?.findings[0]).toEqual({
      code: "unknown",
      severity: "warning",
      line: null,
      message: "Still worth showing.",
    });
  });

  it("reports truncation only when the backend said so", () => {
    expect(runValidation(run({ validation: CLEAN }))?.truncated).toBe(false);
    expect(
      runValidation(run({ validation: { ...CLEAN, findings_truncated: true } }))?.truncated
    ).toBe(true);
  });
});

describe("validationLabel", () => {
  it("names the spec, and counts notes separately from the verdict", () => {
    expect(validationLabel({ ...base(), verdict: "clean" })).toBe("Valid llms.txt");
    expect(validationLabel({ ...base(), verdict: "warnings", warningCount: 1 })).toBe(
      "Valid llms.txt · 1 note"
    );
    expect(validationLabel({ ...base(), verdict: "warnings", warningCount: 3 })).toBe(
      "Valid llms.txt · 3 notes"
    );
    expect(validationLabel({ ...base(), verdict: "errors", errorCount: 1 })).toBe("1 spec error");
    expect(validationLabel({ ...base(), verdict: "errors", errorCount: 2 })).toBe("2 spec errors");
  });
});

function base() {
  return {
    verdict: "clean" as const,
    errorCount: 0,
    warningCount: 0,
    findings: [],
    truncated: false,
  };
}
