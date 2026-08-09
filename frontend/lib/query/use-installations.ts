"use client";

import { useQuery } from "@tanstack/react-query";

import { listInstallations } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `GET /github/installations`. Does not poll.
 *
 * The list changes for exactly two reasons — the user completes an installation (which arrives via
 * a full page navigation back from GitHub, so this query mounts fresh) or disconnects one (which
 * `use-disconnect-installation.ts` invalidates). Neither is a state that resolves on a timer, so
 * polling would be the "looks like a feature, does nothing" waste `use-schedule.ts` describes.
 */
export function useInstallations() {
  return useQuery({
    queryKey: queryKeys.installations.list,
    queryFn: listInstallations,
  });
}
