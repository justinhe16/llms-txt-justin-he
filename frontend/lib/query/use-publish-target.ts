"use client";

import { useQuery } from "@tanstack/react-query";

import { getPublishTarget } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/publish-target`. Does not poll, for the same reason `useSchedule` does not: a
 * target has no in-progress state to wait out, and it changes only when a person writes it — which
 * `use-put-publish-target.ts` already invalidates.
 *
 * Resolves `null` for a website that publishes nowhere. That is the normal state, not an error.
 */
export function usePublishTarget(websiteId: string) {
  return useQuery({
    queryKey: queryKeys.publishTargets.detail(websiteId),
    queryFn: () => getPublishTarget(websiteId),
  });
}
