// Who added a website, rendered as a real GitHub identity for EVERY owner — not just the
// signed-in user.
//
// THE GAP THIS FILE USED TO WORK AROUND IS CLOSED. Before the backend joined `auth.users`
// (`backend/app/features/websites/internals/websites_reader.py`'s `_OWNER_JOIN`), `GET
// /websites?include=latest_run` returned a bare `user_id` and nothing else about the
// owner, so a real handle was renderable for exactly one person: you, read out of your own
// Supabase session (`lib/auth/use-user.ts`). Every `WebsiteListItem`/`WebsiteResponse` now
// carries an `owner` object — `handle`, `display_name`, `avatar_url`, each individually
// nullable (`lib/api/websites.ts`'s `Owner`) — resolved server-side from the SAME two
// Supabase Auth metadata fallback chains `use-user.ts` applies for the signed-in user
// (`user_name` then `preferred_username` for `handle`; `full_name` then `name` for
// `display_name`). This file's job shrinks to exactly one thing: turning that `owner`
// object — plus, for your own row, your live session — into what the Owner pill renders.
//
// The fallback chain for anybody who is not you: `@handle`, then `display_name`, then the
// honest eight-character short id this file has always fallen back to. Never a fabricated
// handle — a string that merely LOOKS like `@something` but names nobody is worse than an
// honest short id, because it is the kind of thing someone copies into a search box — and
// never an email; `Owner` does not carry one (see that module's own docstring for why).
// `owner: null` (no `auth.users` row at all, or one with none of the three fields set) and
// an `owner` object whose three fields are all `null` are the same case as far as this file
// is concerned: both fall all the way through to the short id.
//
// Your own row is UNCHANGED by any of this: `isYou` still renders "you" plus your own
// session's avatar/handle, read from `useUser()` rather than from `owner`, so it updates
// the instant your session does instead of waiting on this table's next fetch.

import type { Owner } from "@/lib/api/websites";
import { initials } from "@/lib/auth/initials";
import type { AuthUser } from "@/lib/auth/use-user";

/** The rendered identity of a website's owner — the plain-text form the four "only the
 * owner (…) can …" messages (schedule-tab.tsx, enrichment-panel.tsx, run-now-button.tsx,
 * publish-panel.tsx) share, by way of `website-detail.tsx`'s own `ownerLabel`, which is the
 * one place in the app that calls this function (see that component's docstring for why
 * the other four do not each call it independently). */
export interface OwnerIdentity {
  /** True when the signed-in user added this website. */
  isYou: boolean;
  /** What a sentence should call this owner: "you", `@handle`, a display name, or — when
   * nothing else is known — the eight-character short id. */
  label: string;
  /** The full `user_id`, for a caller that wants the unambiguous form regardless of
   * `label`. */
  userId: string;
}

/** Enough of the UUID to tell two unidentified owners apart at a glance, and short enough
 * not to break the column. Eight hex characters is ~4 billion values; a collision inside
 * one page of this table is not a real concern, and a tooltip carries the full id
 * regardless. */
const SHORT_ID_LENGTH = 8;

function shortId(userId: string): string {
  return userId.replace(/-/g, "").slice(0, SHORT_ID_LENGTH);
}

/** `owner`'s two-field fallback, falling all the way through to the short id. `Owner`'s
 * `handle`/`display_name` are themselves already the RESOLVED form of the two Supabase
 * metadata pairs (`user_name`/`preferred_username`, `full_name`/`name`) —
 * `websites_reader.py`'s `_OWNER_COLUMNS` and `service.py`'s `_owner_from_row` did that
 * work server-side — so there is only the one fallback left to apply here. */
function resolvedLabel(userId: string, owner: Owner | null): string {
  if (owner !== null && owner.handle !== null) return `@${owner.handle}`;
  if (owner !== null && owner.display_name !== null) return owner.display_name;
  return shortId(userId);
}

/**
 * `currentUserId` is `null` while `useUser()` is still resolving the session, or if there
 * is somehow no session at all. Every row then renders as somebody else's, which is the
 * right way round to be wrong for the half-second it lasts: labelling a stranger's row
 * "you" is a claim about ownership, and labelling your own row with its short id is only
 * less friendly.
 *
 * `owner` defaults to `null` rather than being required, so a caller that only ever means
 * "am I looking at my own row" (there is currently none, but the signature should not force
 * one into existence) is not obliged to thread one through.
 */
export function ownerIdentity(
  userId: string,
  currentUserId: string | null,
  owner: Owner | null = null
): OwnerIdentity {
  const isYou = currentUserId !== null && currentUserId === userId;
  return {
    isYou,
    label: isYou ? "you" : resolvedLabel(userId, owner),
    userId,
  };
}

/** The fields of `AuthUser` (lib/auth/use-user.ts) this module reads. Structural, so
 * lib/crawls/ depends on four named fields rather than on the whole auth shape. */
export type OwnerViewer = Pick<AuthUser, "id" | "handle" | "avatarUrl" | "displayName">;

/** What the Owner pill renders. Avatar-or-initial plus `text` for EVERY owner now, not only
 * the signed-in user's own row — the asymmetry this module used to have is gone along with
 * the gap that caused it. */
export interface OwnerPill {
  /** True only when the signed-in user added this website. False while the session is
   * still resolving, for the reason `ownerIdentity` documents. */
  isYou: boolean;
  /** The pill's own text: "you", `@handle`, a display name, or the short id. */
  text: string;
  /** The tooltip's content: the owner's `@handle` whenever one is known — yours read from
   * your own session, everybody else's from `owner` — and the full `user_id` only when no
   * handle exists at all. Hovering any pill therefore answers the same question about
   * whoever it names, which is the point: a tooltip that showed a handle on your row and a
   * raw UUID on the next one read as leftover plumbing rather than as a deliberate
   * difference.
   *
   * On a row whose `text` is already the handle this repeats it, and that redundancy is
   * accepted deliberately: the alternative rules (suppress the tooltip, or swap in the
   * display name) make what a hover means depend on which fields that particular owner
   * happens to have, and an inconsistent tooltip is worse than a redundant one. The rows
   * where it still earns its keep are the ones with no handle — a display name, or the
   * short id, either of which the full `user_id` genuinely expands on. */
  tooltip: string;
  /** True when `tooltip` above is the raw `user_id` rather than an `@handle` — the one case
   * worth rendering in monospace, the same way the short id itself is. False on any row
   * whose owner has a handle, yours or anyone's; true on the rest (an owner known only by
   * display name, one with no Auth metadata at all, and your own row on the local dev
   * password sign-in path, which gives a session no handle). */
  tooltipIsRawId: boolean;
  /** The full `user_id`. */
  userId: string;
  /** An avatar image URL — yours from your own session, anyone else's from `owner` — or
   * `null` when none is known. */
  avatarUrl: string | null;
  /** The letter drawn in place of a missing avatar, when SOME identity (a handle or a
   * display name — yours, or `owner`'s) is known. `null` when the pill has nothing better
   * than the short id, since a monogram implies a name that does not exist. */
  initial: string | null;
  /** True when `text` is a real name or handle rather than the raw short id. Drives the
   * pill's styling: a short id reads as an id (muted, monospace); a real name or handle
   * does not. */
  isIdentified: boolean;
}

export function ownerPill(userId: string, viewer: OwnerViewer | null, owner: Owner | null): OwnerPill {
  const isYou = viewer !== null && viewer.id === userId;

  if (isYou) {
    return {
      isYou: true,
      text: "you",
      tooltip: viewer.handle !== null ? `@${viewer.handle}` : userId,
      tooltipIsRawId: viewer.handle === null,
      userId,
      avatarUrl: viewer.avatarUrl,
      initial: viewer.avatarUrl === null ? initials(viewer.displayName) : null,
      isIdentified: true,
    };
  }

  const handle = owner?.handle ?? null;
  const displayName = owner?.display_name ?? null;
  const avatarUrl = owner?.avatar_url ?? null;
  const isIdentified = handle !== null || displayName !== null;
  const text = handle !== null ? `@${handle}` : (displayName ?? shortId(userId));

  return {
    isYou: false,
    text,
    tooltip: handle !== null ? `@${handle}` : userId,
    tooltipIsRawId: handle === null,
    userId,
    avatarUrl,
    initial: avatarUrl === null && isIdentified ? initials(displayName ?? handle ?? "") : null,
    isIdentified,
  };
}
