// The view model behind the Output tab's conformance strip
// (components/crawls/validation-summary.tsx) — one run's `stats.validation` turned into a
// verdict, a label, and a list of findings.
//
// Reads `run.stats` the same defensive `?.`/`typeof`/`Array.isArray` way
// `run-provenance.ts` and `run-display.ts` do, for the identical reason: `stats` is jsonb
// whose shape belongs to the crawler feature, typed loosely on the wire
// (`{ [key: string]: unknown } | null`), so a client reads it defensively rather than
// assuming a shape that may not be there. Every run crawled before the validation ticket
// deployed has no `validation` key at all, and that is a state this module has to render as
// "we did not check" — never as "it passed".
//
// **`conforms` is not "no findings", and this module never conflates the two.** llmstxt.org
// requires nothing but an H1, so a spec-conformant artifact can still carry warnings about
// being a poor one (`backend/app/features/crawl/internals/validate.py` argues this in full).
// The three verdicts below are what keep that distinction visible in the UI instead of
// collapsing it into a single pass/fail dot.

import type { RunDetail } from "@/lib/api/runs";

/** One finding, mirroring the backend's `{code, severity, line, message}`. `line` is 1-based,
 * and `null` for a finding about the document as a whole rather than about one line. */
export interface ValidationFinding {
  code: string;
  severity: "error" | "warning";
  line: number | null;
  message: string;
}

/**
 * What this run's index was found to be.
 *
 * * `"unavailable"` — no `validation` block on the row. Either the run predates the check or
 *   it never produced an index (a seed failure records `validation: null`). Renders as
 *   nothing at all rather than as a neutral badge: a strip reading "not checked" on every
 *   historical run would be noise on rows nobody can do anything about.
 * * `"clean"` — conformant, and no warnings either. The only state that is unambiguously good.
 * * `"warnings"` — conformant, with notes. The spec is satisfied; the artifact could be better.
 * * `"errors"` — not conformant. Something the spec states is violated.
 */
export type ValidationVerdict = "unavailable" | "clean" | "warnings" | "errors";

export interface RunValidation {
  verdict: Exclude<ValidationVerdict, "unavailable">;
  errorCount: number;
  warningCount: number;
  findings: ValidationFinding[];
  /** Whether the backend's `MAX_FINDINGS` cap trimmed `findings`. The counts above stay exact
   * either way, so the strip can say "showing 25 of 60" rather than quietly under-reporting. */
  truncated: boolean;
}

/**
 * This run's conformance report, or `null` when the row carries none.
 *
 * `null` covers three different things on purpose — no `validation` key (a pre-ticket row), an
 * explicit `validation: null` (a run that produced no index to check), and a `validation` value
 * of some shape this function does not recognise. None of the three is a claim about the
 * artifact, and the UI treats all three the same way: it says nothing. Distinguishing them
 * would mean rendering three flavours of "no information," which is two more than a reader
 * needs.
 */
export function runValidation(run: RunDetail): RunValidation | null {
  const raw = run.stats?.validation;
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return null;

  const block = raw as Record<string, unknown>;
  if (typeof block.conforms !== "boolean") return null;

  const errorCount = asCount(block.error_count);
  const warningCount = asCount(block.warning_count);
  const findings = Array.isArray(block.findings) ? block.findings.flatMap(asFinding) : [];

  return {
    // Derived from `conforms` rather than from `errorCount > 0`, because `conforms` is the
    // backend's own verdict and the counts are a summary of it. If the two ever disagreed, the
    // verdict is the one that decided whether the artifact violates the spec.
    verdict: !block.conforms ? "errors" : warningCount > 0 ? "warnings" : "clean",
    errorCount,
    warningCount,
    findings,
    truncated: block.findings_truncated === true,
  };
}

/** A non-negative integer from an unknown, or `0`. Mirrors `runPagesCrawled`'s guard in
 * `run-display.ts`: jsonb can hold anything, and `NaN` rendered into a label reads as a bug in
 * the page rather than as missing data. */
function asCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.trunc(value) : 0;
}

/**
 * One finding from an unknown, as a 0-or-1-element array so callers can `flatMap` it.
 *
 * Returns `[]` for anything missing the two fields that make a finding renderable — a `message`
 * to show and a `severity` to colour it by. A malformed entry is dropped rather than rendered
 * as an empty row: the counts come from `error_count`/`warning_count`, which are the backend's
 * own totals and are unaffected by a single unparseable entry here.
 */
function asFinding(value: unknown): ValidationFinding[] {
  if (typeof value !== "object" || value === null) return [];
  const finding = value as Record<string, unknown>;
  if (typeof finding.message !== "string") return [];
  if (finding.severity !== "error" && finding.severity !== "warning") return [];

  return [
    {
      code: typeof finding.code === "string" ? finding.code : "unknown",
      severity: finding.severity,
      line: typeof finding.line === "number" && Number.isFinite(finding.line) ? finding.line : null,
      message: finding.message,
    },
  ];
}

/**
 * The strip's one-line summary.
 *
 * Written so the SPEC is what the sentence is about, not our own opinion of the file: "Valid
 * llms.txt" states which standard was met, and the notes are counted separately beside it
 * rather than folded into a score. A number a user cannot trace back to a rule is a number
 * they cannot act on.
 */
export function validationLabel(validation: RunValidation): string {
  switch (validation.verdict) {
    case "clean":
      return "Valid llms.txt";
    case "warnings":
      return `Valid llms.txt · ${count(validation.warningCount, "note")}`;
    case "errors":
      return count(validation.errorCount, "spec error");
  }
}

/** "1 note" / "2 notes" — pluralized by the number in front of it. */
function count(value: number, noun: string): string {
  return `${value} ${noun}${value === 1 ? "" : "s"}`;
}
