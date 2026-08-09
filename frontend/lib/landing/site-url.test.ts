import { describe, expect, it } from "vitest";

import { parseSiteUrl } from "./site-url";

// The app's front door: every website in the product enters through this one function, and a
// wrong answer here is either a 422 the user cannot act on or a round trip that never had a
// chance. Each rejection carries its own message on purpose ("Invalid URL" tells a user
// nothing about which of the four things they got wrong), so the messages are asserted, not
// just the `ok` flag.

describe("parseSiteUrl", () => {
  it.each([
    "https://example.com",
    "http://example.com",
    "http://localhost:3000/docs",
    "https://example.com/a?b=c",
    "https://sub.domain.example.com/deep/path#anchor",
  ])("accepts %s", (input) => {
    expect(parseSiteUrl(input)).toEqual({ ok: true, url: input });
  });

  it("trims surrounding whitespace and submits the trimmed URL", () => {
    expect(parseSiteUrl("  https://example.com  ")).toEqual({
      ok: true,
      url: "https://example.com",
    });
  });

  it("rejects an empty input", () => {
    expect(parseSiteUrl("")).toEqual({ ok: false, message: "Enter a website URL." });
    expect(parseSiteUrl("   ")).toEqual({ ok: false, message: "Enter a website URL." });
  });

  it("rejects a bare hostname rather than guessing a scheme", () => {
    // Deliberate: prepending `https://` would be helpful right up until it guesses wrong
    // about a host only served over plain http, and it would make the field's contents
    // differ from what gets sent.
    const result = parseSiteUrl("example.com");

    expect(result.ok).toBe(false);
    expect(result).toEqual({ ok: false, message: "Enter a full URL, including https://" });
  });

  it.each(["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)"])(
    "rejects the non-http(s) scheme in %s",
    (input) => {
      const result = parseSiteUrl(input);

      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.message).toContain("http://");
    }
  );

  it("rejects a URL that parses but has no host", () => {
    const result = parseSiteUrl("file:///x");

    expect(result.ok).toBe(false);
  });

  it("rejects a URL longer than the 2048 the API declares", () => {
    // One over the limit, so this fails if the comparison is ever written as `>=`.
    const tooLong = `https://example.com/${"a".repeat(2048)}`;
    const result = parseSiteUrl(tooLong);

    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.message).toContain("2048");
  });

  it("accepts a URL exactly at the limit", () => {
    const prefix = "https://example.com/";
    const exact = prefix + "a".repeat(2048 - prefix.length);

    expect(exact).toHaveLength(2048);
    expect(parseSiteUrl(exact).ok).toBe(true);
  });
});
