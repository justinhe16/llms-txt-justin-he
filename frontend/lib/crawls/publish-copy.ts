// Every string a human wrote about a publication's outcome, and the token that colours it.
//
// Split from `lib/query/` and from the component for the same reason `provenance-copy.ts` is split
// from `run-provenance.ts` (ARCHITECTURE.md §8.4): the copy is what a reader edits, and it should
// not sit inside a component's render tree or a data hook.

import type { PublishStatus } from "@/lib/api/publish";

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
