// Named, feature-level wrappers around `api` (lib/api/fetcher.ts) for GitHub installations,
// publish targets, and publication history. Every call site — a component, a React Query hook —
// imports from here rather than reaching for `api.get`/`api.put` directly, so this file plus
// lib/api/websites.ts, lib/api/runs.ts, lib/api/schedules.ts and lib/api/health.ts remain the
// entire inventory of what the frontend can ask the backend for.
//
// **Two id spaces, and confusing them is the mistake this file is arranged to prevent.** An
// installation has BOTH a GitHub numeric id (`installation_id` on the response — what GitHub's
// setup callback hands us) and this API's own uuid (`id` — what a publish target references).
// Every function below takes whichever one its endpoint actually wants, and the parameter names
// say which: `installationRowId` for the uuid, `githubInstallationId` for GitHub's number.
//
// Nothing here holds or transmits a token. The installation id is not a credential — it grants
// nothing without the deployment's own GitHub App private key, which lives only on the server.
//
// **`POST /github/installations` is deliberately absent from this file.** It is called once, from
// `app/api/github/callback/route.ts`, which runs on the SERVER — and `lib/api/fetcher.ts` resolves
// every path against a relative `/api` prefix that "only means anything in the browser". Server
// code talks to `API_URL` directly, which is what that route does. A wrapper here would be a
// browser helper for an endpoint no browser ever calls.

import type { components } from "./schema";
import { api } from "./fetcher";

// Readable aliases, each a straight indexed access into the generated type — never a hand-written
// shape — so a field the backend adds shows up here on the next `npm run gen:api`.
export type Installation = components["schemas"]["InstallationResponse"];
export type PublishTarget = components["schemas"]["PublishTargetResponse"];
export type PublishTargetRequest = components["schemas"]["PublishTargetRequest"];
export type Publication = components["schemas"]["PublicationResponse"];
export type Repository = components["schemas"]["RepositoryResponse"];
export type RepositoryList = components["schemas"]["RepositoryListResponse"];

/** `publish_mode` — how a publication writes. Read off the request type rather than re-listed as
 * a literal union, so a third mode added upstream is a `tsc` error here instead of a silent gap. */
export type PublishMode = PublishTargetRequest["mode"];

/** `publish_status` — how one attempt ended. `skipped_unchanged` is a success: the system looked
 * and the index was identical. */
export type PublishStatus = Publication["status"];

/**
 * `GET /github/installations`. **Per-user, unlike every other read in this API** — an installation
 * is a fact about someone's GitHub account rather than about a crawled site, so the backend scopes
 * it by caller (see `internals/publish_reader.py`'s note on ARCHITECTURE.md §4.1).
 */
export function listInstallations(): Promise<Installation[]> {
  return api.get("/github/installations");
}

/**
 * `DELETE /github/installations/{id}`. Forgets the installation and every publish target that used
 * it. `404` when the id names no installation of this user's.
 *
 * Does **not** uninstall the App on GitHub — that is the user's own action in their account
 * settings, and this API deliberately cannot do it on their behalf.
 */
export function disconnectInstallation(installationRowId: string): Promise<void> {
  return api.delete("/github/installations/{id}", { params: { id: installationRowId } });
}

/**
 * `GET /github/installations/{id}/repositories`. Proxied live from GitHub rather than stored,
 * because the set changes whenever the user edits the installation's repository access.
 *
 * `truncated` on the response means the installation has more repositories than one page carries;
 * a caller that sees it should let the user type a name instead of presenting a prefix as the whole
 * list. `502` when GitHub cannot be reached.
 */
export function listRepositories(installationRowId: string): Promise<RepositoryList> {
  return api.get("/github/installations/{id}/repositories", {
    params: { id: installationRowId },
  });
}

/**
 * `GET /websites/{id}/publish-target`. Unscoped, like every website-scoped read.
 *
 * Resolves `null` — not a thrown `ApiError` — when the website publishes nowhere. That is the
 * OpenAPI document's own answer (a genuine `anyOf`), matching how `getSchedule` treats a website
 * with no schedule: a `200` with a `null` body is the normal "nothing configured" state, and `404`
 * is reserved for an unknown website.
 */
export function getPublishTarget(websiteId: string): Promise<PublishTarget | null> {
  return api.get("/websites/{id}/publish-target", { params: { id: websiteId } });
}

/**
 * `PUT /websites/{id}/publish-target`. Owner only — `403` otherwise. Always upserts, so there is
 * no separate create-vs-update call, matching the single service method behind the route.
 *
 * `body.installation_id` is this API's uuid, not GitHub's number — see this file's header.
 */
export function putPublishTarget(
  websiteId: string,
  body: PublishTargetRequest
): Promise<PublishTarget> {
  return api.put("/websites/{id}/publish-target", { params: { id: websiteId }, body });
}

/** `DELETE /websites/{id}/publish-target`. Owner only. Idempotent — `204` even if there was none. */
export function deletePublishTarget(websiteId: string): Promise<void> {
  return api.delete("/websites/{id}/publish-target", { params: { id: websiteId } });
}

/**
 * `GET /websites/{id}/publications`. Newest first, capped server-side — not paginated, because this
 * is a panel whose list is almost always length zero or one.
 */
export function listPublications(websiteId: string): Promise<Publication[]> {
  return api.get("/websites/{id}/publications", { params: { id: websiteId } });
}
