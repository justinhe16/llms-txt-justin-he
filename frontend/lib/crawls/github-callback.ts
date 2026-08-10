// The pure half of `app/api/github/callback/route.ts` — split out for the same reason
// `lib/auth/next-path.ts` is split out from `app/auth/callback/route.ts`: a redirect target
// built by string interpolation is a security surface, and a security surface belongs in a
// module small enough to read completely and test exhaustively, not inline in a handler that
// also does I/O. Read `next-path.ts` first; this is the same shape applied to a second field.
//
// ## The security property, stated once so every test below can be read against it
//
// `githubCallbackPath` always returns a string that starts with `/crawls`, and the only piece
// of it built from caller-supplied input is a segment that matched
// `/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i` first. No input — however
// hostile — produces an absolute URL, a protocol-relative `//host`, a `..` path segment, a
// backslash, a control character, or a second `?` or `&` outside the one query string this
// function itself builds with `URLSearchParams`. `state` is exactly as attacker-suppliable as
// `installation_id` already is (see the route's own header comment): it arrives on a redirect
// GitHub issues after a browser round trip we do not control the query string of, so this
// module treats it as hostile by default and earns the site-scoped redirect only by matching
// the shape above.

import type { DetailTab } from "./detail-tabs";

/** The tab a successful (or failed) connect attempt returns the user to. A typed constant,
 * not a literal repeated at each call site — renaming a tab is then a `tsc` error here,
 * caught before it ships, rather than a redirect that silently falls back to Runs because the
 * string it wrote no longer matches anything `isDetailTab` recognizes. */
const PUBLISH_TAB: DetailTab = "publish";

/** The five outcomes `app/api/github/callback/route.ts` can end in. */
export type GithubCallbackResult =
  | "connected"
  | "cancelled"
  | "missing_installation"
  | "not_signed_in"
  | "verify_failed";

/** The query parameter the callback route sets and `GithubConnectToast` reads. */
export const GITHUB_RESULT_PARAM = "github";

const RESULTS: readonly GithubCallbackResult[] = [
  "connected",
  "cancelled",
  "missing_installation",
  "not_signed_in",
  "verify_failed",
];

/** Narrows an arbitrary `?github=` value to one of ours, or `null`. */
export function isGithubCallbackResult(value: string | null): value is GithubCallbackResult {
  return value !== null && (RESULTS as readonly string[]).includes(value);
}

// Shape validation only, never the version nibble. `db/schema.prisma`'s `websites.id` is
// `gen_random_uuid()` — a v4 UUID today — but a regex that also checked the version and
// variant bits would silently start rejecting every `state` the day that default changes,
// which is a worse failure than accepting a syntactically-UUID-shaped string that happens not
// to be one: a made-up id that matches this shape still only ever reaches `/crawls/{id}`, a
// route that 404s on an id no website has. Hex and hyphens, 8-4-4-4-12, case-insensitive,
// anchored at both ends.
//
// The anchoring is doing more work here than it would in Python. JavaScript's `$` — unlike
// Python's `re.search`/`re.match`, which also match just before a single trailing `\n` — only
// matches the true end of the string when the `m` flag is absent, as it is here. So a `state`
// of a valid UUID plus a trailing newline fails this pattern for two independent reasons
// (there is no `m` flag, and there is trailing content after the 36th character either way),
// not because of a subtlety that has to be remembered — but a reviewer porting this pattern to
// a language whose `$` is Python's would need to add `\Z` to keep the same guarantee, so it is
// written down here rather than assumed.
const STATE_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The SITE-RELATIVE path GitHub's callback redirects to. Never an absolute URL — the route
 * composes `${base}${githubCallbackPath(...)}`, keeping host resolution (the
 * `x-forwarded-host` handling the route's own header comment describes) in the one file that
 * already reasons about it. `githubCallbackPath` itself has no notion of a host at all.
 *
 * A `state` that matches `STATE_PATTERN` sends the user back to that website's Publish tab,
 * carrying the outcome for `GithubConnectToast` to read there. Anything else — absent,
 * malformed, or actively hostile — falls back to the flat `/crawls` list: an unrecognized
 * `state` is exactly as untrustworthy as no `state` at all, so the two share one fallback
 * rather than the malformed case getting special handling that itself becomes a second surface
 * to get right.
 */
export function githubCallbackPath(
  result: GithubCallbackResult,
  state: string | null | undefined
): string {
  const params = new URLSearchParams({ tab: PUBLISH_TAB, github: result });

  if (state !== null && state !== undefined && STATE_PATTERN.test(state)) {
    return `/crawls/${state}?${params.toString()}`;
  }

  params.delete("tab");
  return `/crawls?${params.toString()}`;
}
