// Where GitHub sends a user back after they install (or reconfigure) the llms-text GitHub App.
//
// This is a genuine Next-native API route sitting alongside `app/api/[...path]/route.ts`, not
// through it. That catch-all's own header comment describes exactly this case: "anything more
// specific than `app/api/[...path]/` wins over this file, so add it alongside, not instead of,
// this one." `/api/github/callback` is more specific, so Next matches it first.
//
// It has to be a route rather than a page because GitHub performs a top-level browser
// NAVIGATION here, not a fetch — so there is no client-side code running to read the query
// string, and the response has to be a redirect a browser follows.
//
// ## What arrives, and why none of it is trusted
//
// GitHub appends `installation_id` and `setup_action` to this URL. Both are query parameters on
// a redirect, which makes them attacker-suppliable: anyone can navigate a signed-in user to
// `/api/github/callback?installation_id=12345`. This route therefore does not treat the number
// as a fact — it hands it to `POST /github/installations`, which verifies it against GitHub with
// the deployment's own App credential before writing a row. See
// `PublishService.connect_installation`.
//
// The number is not a secret either, so there is nothing here to redact. An installation id
// grants nothing without the App private key, which lives only on the Fly host.

import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

/** Where a user lands after this route finishes, in every outcome. */
const LANDING_PATH = "/crawls";

/** Bounds how long this handler waits on Fly, mirroring the `[...path]` proxy's own budget. A
 * wedged upstream becomes a redirect carrying `verify_failed` rather than a hung navigation. */
const UPSTREAM_TIMEOUT_MS = 30_000;

/**
 * Query-parameter values this route sets on `LANDING_PATH` so the page can toast the outcome.
 *
 * Fixed codes rather than a message, for the same reason `app/auth/callback/route.ts` maps its
 * own failures to a small vocabulary: `error_description` on an external redirect is untrusted
 * text and must never be rendered, so the page maps a code it recognizes to copy we wrote.
 */
type CallbackResult =
  | "connected"
  | "cancelled"
  | "missing_installation"
  | "not_signed_in"
  | "verify_failed";

export async function GET(request: NextRequest): Promise<Response> {
  const { searchParams, origin } = request.nextUrl;

  // Computed the way `app/auth/callback/route.ts` computes its own, and safe for the same
  // narrow reason: Vercel sets `x-forwarded-host` at its edge and overwrites any client value,
  // and this app deploys nowhere else. Behind a proxy that forwards the client's header, this
  // becomes a host-header open redirect — validate against an allow-list before moving it.
  const forwardedHost = request.headers.get("x-forwarded-host");
  const base =
    process.env.NODE_ENV === "development"
      ? origin
      : forwardedHost
        ? `https://${forwardedHost}`
        : origin;

  // `setup_action=request` means the user asked an organization owner to approve the install
  // rather than completing one. There is no installation to record yet, and treating it as a
  // failure would be wrong — nothing went wrong, it is simply pending someone else.
  const setupAction = searchParams.get("setup_action");
  if (setupAction === "request") {
    return done(base, "cancelled");
  }

  const raw = searchParams.get("installation_id");
  const installationId = Number(raw);
  // `Number("")` is 0 and `Number(null)` is 0, so the emptiness check has to come first — and
  // `Number.isSafeInteger` rather than `isInteger` because GitHub's ids are 64-bit and a value
  // past 2^53 cannot survive a round trip through a JavaScript number without silently
  // changing. Rejecting it here is better than sending a subtly different id to the API.
  if (raw === null || raw === "" || !Number.isSafeInteger(installationId) || installationId <= 0) {
    return done(base, "missing_installation");
  }

  // The user must be signed in for the installation to belong to anybody. This is the same
  // check the proxy makes, made here too because this route does not go through it: without a
  // session there is no `user_id` to attach the installation to, and the API would answer 401
  // with a body no browser navigation will ever display.
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (user === null) {
    return done(base, "not_signed_in");
  }

  // `API_URL` DIRECTLY, not through `lib/api/fetcher.ts` and not through the `[...path]` proxy.
  // That module resolves every path against a relative `/api` prefix which, as its own header
  // says, "only means anything in the browser" — this handler runs on the server, so it does what
  // that comment prescribes: "Server code that needs the API talks to API_URL directly."
  //
  // Read inside the handler rather than at module scope, for the reason the proxy gives for its
  // own lazy read: a missing `API_URL` must not take `next build` down for every page.
  const apiUrl = process.env.API_URL;
  if (apiUrl === undefined || apiUrl === "") {
    console.error("github callback: API_URL is not configured");
    return done(base, "verify_failed");
  }

  try {
    const response = await fetch(
      `${apiUrl.replace(/\/$/, "")}/github/installations?installation_id=${installationId}`,
      {
        method: "POST",
        headers: {
          // The one place this route handles a token, and it happens entirely server-side —
          // ARCHITECTURE.md §8.2. `getSession` is safe here because `getUser` above already
          // verified and refreshed the session, so this reads the current token from memory
          // rather than trusting a cookie (the same ordering the proxy uses).
          authorization: `Bearer ${(await supabase.auth.getSession()).data.session?.access_token}`,
          accept: "application/json",
        },
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      }
    );
    if (!response.ok) {
      // Every refusal collapses to one outcome on purpose. A `400` means GitHub did not recognize
      // the installation (a hand-typed id), a `503` means this deployment has no App configured,
      // and a `401` means the session died mid-flight — and none of them gives the user a
      // different thing to do than "try connecting again".
      //
      // Never log the response body: it is FastAPI's error detail, and a body is not the place to
      // assume nothing sensitive appears. The status is what distinguishes the cases.
      console.error("github callback: API refused the installation", response.status);
      return done(base, "verify_failed");
    }
  } catch (error) {
    // A transport failure, or the timeout above. Logged rather than swallowed: Vercel's function
    // logs are the only record this will ever have.
    console.error("github callback: could not reach the API", error);
    return done(base, "verify_failed");
  }

  return done(base, "connected");
}

/** Redirect to the crawls page, carrying the outcome for it to toast. */
function done(base: string, result: CallbackResult): Response {
  return NextResponse.redirect(`${base}${LANDING_PATH}?github=${result}`);
}
