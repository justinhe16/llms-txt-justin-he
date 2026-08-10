// The tab vocabulary of `/crawls/[websiteId]` — extracted from `use-detail-view.ts`, which
// still owns the URL state built on top of it, because this list itself has a second reader
// now: `app/api/github/callback/route.ts` needs to know a redirect's `?tab=publish` names a
// real tab, and that file is a SERVER route handler. `use-detail-view.ts` is `"use client"`
// and imports `next/navigation` at module scope, so a route handler cannot import from it
// without dragging a client-only module into a server bundle — and neither should a
// `lib/**/*.test.ts` under `environment: "node"` (vitest.config.ts) have to resolve
// `next/navigation` just to check that `isDetailTab("publish")` is `true`. Splitting the
// vocabulary out from the hook that reads and writes it is what lets both consumers use it.

export const DETAIL_TABS = ["runs", "output", "schedule", "publish", "trends"] as const;

export type DetailTab = (typeof DETAIL_TABS)[number];

/** The tab a URL with no (or an unrecognized) `?tab=` resolves to. */
export const DEFAULT_DETAIL_TAB: DetailTab = "runs";

// `publish` sits between `schedule` and `trends`, not appended after `trends`. Appending
// would have been the smaller diff, but it would also move a tab users have already learned
// to find last — Trends stays the last tab, and the new one lands beside Schedule, the tab
// it was extracted out of (`PublishPanel` moved here from a card inside the Schedule tab; see
// that component's own docstring for why the move happened at all).

// A `readonly [...]` tuple's `.includes` only accepts its own member type, so the argument is
// widened to `string` for the check. The predicate signature is what narrows afterwards, and
// it is doing real work in both of this function's callers: on the client, `?tab=` is
// user-supplied text and `<Tabs value="../../etc">` would otherwise put an arbitrary string
// into the tab state; on the server, it is what stops `github-callback.ts` from ever building
// a redirect whose `?tab=` names something that is not one of these five values, however that
// module's own `PUBLISH_TAB` constant is spelled.
export function isDetailTab(value: string | null): value is DetailTab {
  return value !== null && (DETAIL_TABS as readonly string[]).includes(value);
}
