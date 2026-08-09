"use client";

import { useQuery } from "@tanstack/react-query";

import { listPublications } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `GET /websites/{id}/publications`.
 *
 * **This one is worth polling, and it is the only query in this feature that is.** A publication is
 * written by the WORKER, after a crawl completes — nothing in the browser causes it, so no
 * invalidation can learn about it. A user who clicks "Run now" with publishing on is waiting for a
 * pull request to appear, and the only way this panel learns that it did is by asking again.
 *
 * `refetchInterval` is a plain number rather than `pollWhileActive` (lib/query/polling.ts), because
 * that helper reads a run's `status` and a publication list has no in-flight state of its own to
 * read. The interval is longer than the runs poll's 3s: a publication lands after a crawl finishes,
 * so the wait is tens of seconds at best and there is nothing to gain from asking more often.
 */
export function usePublications(websiteId: string) {
  return useQuery({
    queryKey: queryKeys.publications.list(websiteId),
    queryFn: () => listPublications(websiteId),
    refetchInterval: 10_000,
  });
}
