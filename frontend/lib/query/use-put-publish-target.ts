"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { putPublishTarget, type PublishTargetRequest } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `PUT /websites/{id}/publish-target`.
 *
 * **No optimistic update, deliberately unlike `use-put-schedule.ts`.** That hook goes to real
 * trouble to seed the cache from the response, because its panel's controls have already moved and
 * a refetch round-trip would read as the toggle snapping back. This form is submitted with a
 * button and shows a pending state until the server answers, so there is no moved control to keep
 * steady — and the server may legitimately normalize what was sent (`path` is stripped of a leading
 * slash by `PublishTargetRequest`'s validator), which an optimistic write would briefly contradict.
 *
 * Invalidates the website list as well as the target: a row there renders whether a site publishes.
 */
export function usePutPublishTarget() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      websiteId,
      body,
    }: {
      websiteId: string;
      body: PublishTargetRequest;
    }) => putPublishTarget(websiteId, body),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.publishTargets.detail(variables.websiteId),
      });
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      toast.success("Publishing settings saved");
    },
    onError: (error) => toast.error(error.message),
  });
}
