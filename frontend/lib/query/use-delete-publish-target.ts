"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { deletePublishTarget } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/** `DELETE /websites/{id}/publish-target` — stop publishing this site. Idempotent. */
export function useDeletePublishTarget() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (websiteId: string) => deletePublishTarget(websiteId),
    onSuccess: (_data, websiteId) => {
      queryClient.invalidateQueries({ queryKey: queryKeys.publishTargets.detail(websiteId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.websites.all });
      toast.success("Publishing turned off for this site");
    },
    onError: (error) => toast.error(error.message),
  });
}
