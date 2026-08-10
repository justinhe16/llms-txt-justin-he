import { describe, expect, it } from "vitest";

import { isDetailTab } from "./detail-tabs";
import {
  githubCallbackPath,
  isGithubCallbackResult,
  type GithubCallbackResult,
} from "./github-callback";

// `githubCallbackPath` builds a redirect target out of a value GitHub echoes back to us on an
// unauthenticated browser navigation — as attacker-controlled as `installation_id` already is
// (see `app/api/github/callback/route.ts`'s own header comment). A wrong answer here is not a
// cosmetic bug: it is an open redirect or a response-splitting hole reachable by anyone who can
// get a signed-in user to click a link. Every case below is read against the one guarantee
// `github-callback.ts`'s module docstring states — the return value always starts with
// `/crawls`, and the only interpolated segment is one that matched the UUID shape first — and
// each assertion checks the *whole* returned string, not a substring, so a correct prefix can't
// hide a wrong suffix.

const ALL_RESULTS: readonly GithubCallbackResult[] = [
  "connected",
  "cancelled",
  "missing_installation",
  "not_signed_in",
  "verify_failed",
];

const VALID_UUID = "11111111-1111-1111-1111-111111111111";

describe("githubCallbackPath — a valid UUID state", () => {
  it.each(ALL_RESULTS)("routes %s to that website's Publish tab", (result) => {
    expect(githubCallbackPath(result, VALID_UUID)).toBe(
      `/crawls/${VALID_UUID}?tab=publish&github=${result}`
    );
  });

  it("accepts an uppercase UUID", () => {
    const upper = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE";
    expect(githubCallbackPath("connected", upper)).toBe(
      `/crawls/${upper}?tab=publish&github=connected`
    );
  });
});

describe("githubCallbackPath — no usable state", () => {
  it.each(ALL_RESULTS)("falls back to the flat list for %s when state is null", (result) => {
    expect(githubCallbackPath(result, null)).toBe(`/crawls?github=${result}`);
  });

  it.each(ALL_RESULTS)("falls back to the flat list for %s when state is undefined", (result) => {
    expect(githubCallbackPath(result, undefined)).toBe(`/crawls?github=${result}`);
  });

  it.each(ALL_RESULTS)("falls back to the flat list for %s when state is empty", (result) => {
    expect(githubCallbackPath(result, "")).toBe(`/crawls?github=${result}`);
  });
});

// Each entry is shaped to defeat one specific mistake in the pattern this module validates
// against, named in its own comment. `state` is never a plain nonsense string — a regex bug
// this narrow needs an input that is ALMOST right to catch it.
const HOSTILE_STATES: readonly string[] = [
  "https://evil.com", // absolute URL, different origin entirely
  "//evil.com", // protocol-relative — browsers treat a leading "//" as same-scheme, new host
  "/\\evil.com", // a leading "/\" some URL parsers treat as "//"
  "..%2f..%2fadmin", // percent-encoded traversal
  "../../etc/passwd", // plain traversal
  `${VALID_UUID}/../../evil`, // a valid UUID prefix — catches a missing trailing `$`
  `x${VALID_UUID}`, // a valid UUID suffix — catches a missing leading `^`
  `${VALID_UUID}\r\nLocation: evil`, // CRLF / response-splitting injection
  `${VALID_UUID}?tab=runs`, // tries to smuggle a second query string
  `${VALID_UUID}&github=connected&evil=1`, // tries to inject a second query parameter
  VALID_UUID.slice(0, -1), // 35 characters — one short
  `1${VALID_UUID}`, // 37 characters — one long, in the first group
  VALID_UUID.replaceAll("-", "_"), // underscores instead of hyphens
  `${VALID_UUID.slice(0, -1)}g`, // a trailing non-hex character
];

describe("githubCallbackPath — hostile state", () => {
  it.each(HOSTILE_STATES)(
    "treats %s as absent: falls back to /crawls, and none of it survives into the redirect",
    (state) => {
      const result = githubCallbackPath("connected", state);

      expect(result).toBe("/crawls?github=connected");
      expect(result).not.toContain(state);
    }
  );

  // Defense in depth beyond the exact-string checks above: for every hostile input, parse the
  // actual returned value as a URL against an arbitrary base and confirm it never resolves off
  // that origin and never leaves the `/crawls` path — the property `github-callback.ts`'s
  // module docstring commits to, checked directly rather than inferred from string equality.
  it.each(HOSTILE_STATES)("never escapes /crawls or the app's own origin for %s", (state) => {
    const result = githubCallbackPath("connected", state);
    const url = new URL(result, "https://app.test");

    expect(url.origin).toBe("https://app.test");
    expect(url.pathname.startsWith("/crawls")).toBe(true);
  });
});

describe("githubCallbackPath — cross-file gate", () => {
  it("the ?tab= it writes is always a real DetailTab", () => {
    const path = githubCallbackPath("connected", VALID_UUID);
    const url = new URL(path, "https://app.test");

    expect(isDetailTab(url.searchParams.get("tab"))).toBe(true);
  });
});

describe("isGithubCallbackResult", () => {
  it.each(ALL_RESULTS)("accepts %s", (result) => {
    expect(isGithubCallbackResult(result)).toBe(true);
  });

  it.each([null, "", "CONNECTED", "connected ", "__proto__"])("rejects %s", (value) => {
    expect(isGithubCallbackResult(value)).toBe(false);
  });
});
