import { describe, expect, it } from "vitest";

import { formatDuration, llmsFullTxtFilename, llmsTxtFilename } from "./run-display";

describe("formatDuration", () => {
  it("uses tenths of a second under a minute", () => {
    expect(formatDuration(0)).toBe("0.0s");
    expect(formatDuration(1_500)).toBe("1.5s");
    expect(formatDuration(59_900)).toBe("59.9s");
  });

  it("switches to minutes and zero-padded seconds at a minute", () => {
    expect(formatDuration(60_000)).toBe("1m 00s");
    expect(formatDuration(90_000)).toBe("1m 30s");
    expect(formatDuration(599_000)).toBe("9m 59s");
  });

  it("switches to hours and zero-padded minutes at an hour", () => {
    expect(formatDuration(3_600_000)).toBe("1h 00m");
    expect(formatDuration(3_900_000)).toBe("1h 05m");
  });

  it("is null for a missing or nonsensical duration", () => {
    // `null` rather than "0.0s": a run with no recorded duration and a run that took no time
    // are different facts, and the caller renders the first as an em dash.
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
    expect(formatDuration(-1)).toBeNull();
  });
});

describe("artifact filenames", () => {
  // These are the SAME strings `backend/app/features/runs/internals/artifact_filename.py`
  // produces — that module's docstring says "byte-for-byte identical to `llmsTxtFilename`",
  // and `backend/tests/test_artifact_filename.py` asserts the same expectations from the
  // other side. Two implementations of one format, so both are pinned to literals rather
  // than to each other's behaviour.

  it("derives a filesystem-safe name from the origin", () => {
    expect(llmsTxtFilename("https://example.com")).toBe("llms-example-com.txt");
    expect(llmsFullTxtFilename("https://example.com")).toBe("llms-full-example-com.txt");
  });

  it("collapses characters a filesystem would object to", () => {
    // `:` is illegal in a filename on Windows and awkward everywhere else; a port and a
    // sub-domain both have to survive as something readable.
    const name = llmsTxtFilename("https://docs.example.com:8443");

    expect(name).toBe("llms-docs-example-com-8443.txt");
    expect(name).not.toContain(":");
  });

  it("lowercases the host", () => {
    expect(llmsTxtFilename("https://EXAMPLE.com")).toBe("llms-example-com.txt");
  });

  it("keeps the two artifacts' names distinct", () => {
    // They are downloaded into the same folder; matching names would make the second
    // silently overwrite the first.
    expect(llmsTxtFilename("https://example.com")).not.toBe(
      llmsFullTxtFilename("https://example.com")
    );
  });

  it("emits only characters that are safe in a quoted Content-Disposition", () => {
    expect(llmsTxtFilename("https://sub.domain.example.co.uk")).toMatch(/^[a-z0-9-]+\.txt$/);
  });
});
