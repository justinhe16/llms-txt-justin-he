"use client";

import { useQuery } from "@tanstack/react-query";

import { listRepositories } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `GET /github/installations/{id}/repositories` — a live proxy to GitHub, for the repository picker.
 *
 * `enabled` is what keeps this from firing on every render of a panel where no installation is
 * selected yet: the endpoint costs a real GitHub round trip through our API, so asking for the
 * repositories of `null` would be a wasted 404.
 *
 * `staleTime` is deliberately non-zero here, unlike every other query in this directory. The
 * repository list is somebody else's data that changes when they edit the installation's access —
 * not something this app ever writes — so there is no invalidation that could keep it fresh, and
 * refetching it on every panel mount would spend a GitHub API call to almost always get the same
 * answer. A minute is short enough that a user who just granted access sees it after one reopen.
 */
export function useRepositories(installationRowId: string | null) {
  return useQuery({
    queryKey: queryKeys.installations.repositories(installationRowId ?? "none"),
    queryFn: () => listRepositories(installationRowId as string),
    enabled: installationRowId !== null,
    staleTime: 60_000,
  });
}
