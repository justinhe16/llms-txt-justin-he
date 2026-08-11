import { describe, expect, it } from "vitest";

import type { Owner } from "../api/websites";
import { ownerIdentity, ownerPill, type OwnerViewer } from "./owner";

// The Owner pill's whole fallback chain, end to end: `@handle`, then a display name, then
// the honest 8-hex-character short id — for everybody who is not the signed-in user, whose
// own row instead renders "you" from their live session and never from `owner` at all.
// `ownerPill` (the pill's rendered shape) and `ownerIdentity` (the plain-text label the four
// "only the owner (…) can …" messages share) apply the identical chain, so both are covered.

const USER_ID = "12345678-90ab-cdef-1234-567890abcdef";

function owner(overrides: Partial<Owner> = {}): Owner {
  return { handle: null, display_name: null, avatar_url: null, ...overrides };
}

function viewer(overrides: Partial<OwnerViewer> = {}): OwnerViewer {
  return {
    id: "viewer-id",
    handle: null,
    avatarUrl: null,
    displayName: "Ada Lovelace",
    ...overrides,
  };
}

describe("ownerPill — somebody else's row", () => {
  it("renders @handle when the backend resolved one", () => {
    const pill = ownerPill(
      USER_ID,
      null,
      owner({ handle: "octocat", display_name: "The Octocat" })
    );

    expect(pill.isYou).toBe(false);
    expect(pill.text).toBe("@octocat");
    expect(pill.isIdentified).toBe(true);
  });

  it("shows the handle in the tooltip too, the same as your own row does", () => {
    // The asymmetry this replaces: every pill but your own used to hover a raw UUID, so the
    // one hover that named a person was the one on the row you already knew.
    const pill = ownerPill(USER_ID, null, owner({ handle: "octocat" }));

    expect(pill.tooltip).toBe("@octocat");
    expect(pill.tooltipIsRawId).toBe(false);
  });

  it("keeps the raw user id in the tooltip for an owner known only by display name", () => {
    // No handle to show, and `text` is already the display name, so the full id is the only
    // thing a hover can add here.
    const pill = ownerPill(USER_ID, null, owner({ display_name: "Ada Lovelace" }));

    expect(pill.tooltip).toBe(USER_ID);
    expect(pill.tooltipIsRawId).toBe(true);
  });

  it("falls back to the display name when there is no handle", () => {
    const pill = ownerPill(USER_ID, null, owner({ display_name: "Ada Lovelace" }));

    expect(pill.text).toBe("Ada Lovelace");
    expect(pill.isIdentified).toBe(true);
  });

  it("falls all the way through to the short id when owner is null", () => {
    const pill = ownerPill(USER_ID, null, null);

    expect(pill.text).toBe("12345678");
    expect(pill.isIdentified).toBe(false);
    expect(pill.tooltip).toBe(USER_ID);
    expect(pill.tooltipIsRawId).toBe(true);
  });

  it("falls all the way through to the short id when owner's three fields are all null", () => {
    // The same case as `owner: null` as far as this module is concerned (owner.ts's own
    // docstring) — asserted separately from the `null` case above so a caller that builds an
    // `Owner` with nothing usable in it, rather than `null` itself, is covered too.
    const pill = ownerPill(USER_ID, null, owner());

    expect(pill.text).toBe("12345678");
    expect(pill.isIdentified).toBe(false);
  });

  it("draws a monogram from the identified name when there is no avatar", () => {
    const pill = ownerPill(USER_ID, null, owner({ handle: "octocat" }));

    expect(pill.avatarUrl).toBeNull();
    expect(pill.initial).toBe("O");
  });

  it("prefers the avatar image over a monogram when one is known", () => {
    const pill = ownerPill(
      USER_ID,
      null,
      owner({ handle: "octocat", avatar_url: "https://example.test/avatar.png" })
    );

    expect(pill.avatarUrl).toBe("https://example.test/avatar.png");
    expect(pill.initial).toBeNull();
  });

  it("draws no monogram at all for an unidentified owner, even with no avatar", () => {
    // A monogram implies a name that does not exist here — the short id is already the
    // pill's whole content, and a letter drawn from it would look like it named someone.
    const pill = ownerPill(USER_ID, null, null);

    expect(pill.avatarUrl).toBeNull();
    expect(pill.initial).toBeNull();
  });
});

describe("ownerPill — the viewer's own row", () => {
  it("renders 'you' from the live session, ignoring whatever the backend sent for this row", () => {
    // The backend's `owner` for the signed-in user's own website is real, resolved data —
    // this endpoint resolves it for every row, not only other people's — so the point of
    // this test is that the pill does not read it: a session update (a fresh avatar, a
    // changed handle) shows up on the next render without waiting on this row's own fetch.
    const stale = owner({ handle: "old-handle", display_name: "Old Name" });
    const pill = ownerPill(USER_ID, viewer({ id: USER_ID, handle: "new-handle" }), stale);

    expect(pill.isYou).toBe(true);
    expect(pill.text).toBe("you");
    expect(pill.tooltip).toBe("@new-handle");
    expect(pill.tooltipIsRawId).toBe(false);
  });

  it("falls back to the raw user id in the tooltip when the session has no handle", () => {
    // The local dev password sign-in path (no GitHub metadata at all) — the one case where
    // "you" still needs the unambiguous form spelled out in full.
    const pill = ownerPill(USER_ID, viewer({ id: USER_ID, handle: null }), null);

    expect(pill.tooltip).toBe(USER_ID);
    expect(pill.tooltipIsRawId).toBe(true);
  });

  it("draws the avatar (or monogram) from the session, never from owner", () => {
    const withAvatar = ownerPill(
      USER_ID,
      viewer({ id: USER_ID, avatarUrl: "https://example.test/me.png", displayName: "Ada" }),
      null
    );
    expect(withAvatar.avatarUrl).toBe("https://example.test/me.png");
    expect(withAvatar.initial).toBeNull();

    const withoutAvatar = ownerPill(
      USER_ID,
      viewer({ id: USER_ID, avatarUrl: null, displayName: "Ada" }),
      null
    );
    expect(withoutAvatar.avatarUrl).toBeNull();
    expect(withoutAvatar.initial).toBe("A");
  });
});

describe("ownerPill — no viewer at all (session still resolving)", () => {
  it("renders the row as somebody else's rather than guessing", () => {
    // `viewer: null` is `CrawlOwner`'s state before `useUser()` resolves — the structural
    // twin of `ownerIdentity`'s `currentUserId === null` below. `isYou` requires a non-null
    // viewer in both functions, so neither can mislabel a stranger's row "you" on a coin
    // flip while the session is still loading.
    const pill = ownerPill(USER_ID, null, owner({ handle: "octocat" }));

    expect(pill.isYou).toBe(false);
    expect(pill.text).toBe("@octocat");
  });
});

describe("ownerIdentity", () => {
  it("renders @handle for somebody else's row", () => {
    const identity = ownerIdentity(USER_ID, "someone-else", owner({ handle: "octocat" }));

    expect(identity.isYou).toBe(false);
    expect(identity.label).toBe("@octocat");
    expect(identity.userId).toBe(USER_ID);
  });

  it("falls back to the display name, then the short id", () => {
    expect(
      ownerIdentity(USER_ID, "someone-else", owner({ display_name: "Ada Lovelace" })).label
    ).toBe("Ada Lovelace");
    expect(ownerIdentity(USER_ID, "someone-else", null).label).toBe("12345678");
  });

  it("renders 'you' for the viewer's own row regardless of what owner carries", () => {
    const identity = ownerIdentity(USER_ID, USER_ID, owner({ handle: "stale-handle" }));

    expect(identity.isYou).toBe(true);
    expect(identity.label).toBe("you");
  });

  it("never renders 'you' while currentUserId is null, even for what will turn out to be your own row", () => {
    // Pinning this function's own documented rationale: `currentUserId` is `null` while the
    // session is still resolving, and every row must render as somebody else's rather than
    // guess — mislabelling a stranger's row "you" is a claim about ownership this function
    // must never make on a coin flip, so it is the wrong direction to be wrong in.
    const identity = ownerIdentity(USER_ID, null, owner({ handle: "octocat" }));

    expect(identity.isYou).toBe(false);
    expect(identity.label).toBe("@octocat");
  });
});
