// Every string a human wrote about a publication's outcome, and the token that colours it.
//
// Split from `lib/query/` and from the component for the same reason `provenance-copy.ts` is split
// from `run-provenance.ts` (ARCHITECTURE.md §8.4): the copy is what a reader edits, and it should
// not sit inside a component's render tree or a data hook.

import type { PublishStatus } from "@/lib/api/publish";
import type { GithubCallbackResult } from "@/lib/crawls/github-callback";

interface PublishStatusCopy {
  label: string;
  /** A `status-*` colour token, never a raw `emerald-*`/`amber-*`/`rose-*` utility — the rule
   * `app/globals.css` states for exactly this kind of mapping. */
  tone: string;
}

/**
 * What one publication status says to a person.
 *
 * **`skipped_unchanged` reads as a success, not as a non-event**, and that wording is the whole
 * point of this table. The system ran, looked, and found the index identical — which on a daily
 * schedule over a stable site is the correct and most common outcome. Labelling it "skipped" alone
 * would make a working setup look like it was doing nothing.
 */
export function publishStatusCopy(status: PublishStatus): PublishStatusCopy {
  switch (status) {
    case "succeeded":
      return { label: "Published", tone: "text-status-completed" };
    case "skipped_unchanged":
      return { label: "No change to publish", tone: "text-status-idle" };
    case "failed":
      return { label: "Couldn't publish", tone: "text-status-failed" };
  }
}

interface GithubConnectCopy {
  /** Which `sonner` call renders it — NOT a colour token like `PublishStatusCopy.tone` above.
   * A publication status is a label sitting permanently in a row; a connect outcome is a
   * one-shot notification, so what varies about it is which of `toast.success` /
   * `toast.info` / `toast.error` `GithubConnectToast` calls, not a class it applies itself. */
  variant: "success" | "info" | "error";
  title: string;
  description: string;
}

/**
 * What each `?github=` outcome — set by `app/api/github/callback/route.ts`, read by
 * `GithubConnectToast` — says to the person who just came back from GitHub, and how loud it
 * says it.
 *
 * `Record<GithubCallbackResult, …>` rather than a `switch`: this file fails to compile the day
 * `github-callback.ts` grows a sixth outcome, until this table says what it looks like. A
 * `switch` with a `default` case would compile silently and ship a blank toast for it instead.
 *
 * Every failure's `description` opens by saying nothing was connected. The bug this feature
 * exists to fix was a user not knowing whether anything had happened after they left the app —
 * burying that answer in the second sentence repeats the original complaint in miniature.
 *
 * `cancelled` is `info`, not `error`, and that is a deliberate departure from the other four:
 * `setup_action=request` means an organization required an owner's approval, so nothing failed
 * and nothing succeeded — it is pending someone else. `components/ui/sonner.tsx` already wires
 * an `InfoIcon` for exactly this shade of outcome, and this is the first call site in the repo
 * that reaches for `toast.info`; the icon was built for a message like this one.
 *
 * `not_signed_in`'s copy is written to read correctly at the moment it is actually seen, which
 * is after a second detour: `middleware.ts` gates `/crawls/[id]`, so this outcome's own redirect
 * bounces again, through `/?next=…`, before landing back on the Publish tab post sign-in. "You
 * weren't signed in" has to still make sense to someone who, from their own point of view, just
 * finished signing in — see R2 in the PR description for the full chain.
 */
export const GITHUB_CONNECT_COPY: Record<GithubCallbackResult, GithubConnectCopy> = {
  connected: {
    variant: "success",
    title: "GitHub connected",
    description:
      'Two steps left: pick a repository and branch below, then turn on "Publish on every successful run."',
  },
  cancelled: {
    variant: "info",
    title: "Approval requested",
    description:
      "Your organization requires an owner to approve this installation. Nothing is connected yet — come back here once it's approved.",
  },
  missing_installation: {
    variant: "error",
    title: "Couldn't connect GitHub",
    description:
      "Nothing was connected — GitHub didn't send back a usable installation. Try connecting again.",
  },
  not_signed_in: {
    variant: "error",
    title: "Couldn't connect GitHub",
    description:
      "You weren't signed in when GitHub sent you back — nothing was connected. Sign in and try connecting again.",
  },
  verify_failed: {
    variant: "error",
    title: "Couldn't connect GitHub",
    description:
      "Nothing was connected — we couldn't verify the installation with GitHub. Try again in a moment.",
  },
};

/**
 * The line `PublishPanel` shows above the form for an account that is connected but has no
 * saved publish target yet (State 2 of that component's own docstring) — both immediately after
 * a fresh `connected` toast and on any later visit before the form is first saved, which is why
 * this does not say "Connected" as if the news just arrived. Kept here rather than inline in
 * JSX for the same reason every other string on this page is, plus one specific to this string:
 * a `.ts` module holding an apostrophe in a rendered `{PUBLISH_NEXT_STEPS}` sidesteps
 * `react/no-unescaped-entities` without an escape sequence in the middle of a sentence.
 */
export const PUBLISH_NEXT_STEPS =
  'You\'re connected to GitHub. Pick a repository and branch below, then turn on "Publish on every successful run" to finish setting up publishing.';
