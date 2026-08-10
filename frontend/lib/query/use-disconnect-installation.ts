"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { disconnectInstallation } from "@/lib/api/publish";

import { queryKeys } from "./query-keys";

/**
 * `DELETE /github/installations/{id}`.
 *
 * Invalidates `installations.all` rather than just the list, which is what sweeps the cached
 * repository lists nested under it — those belong to an installation that no longer exists, and
 * leaving them would let a picker offer repositories we can no longer write to.
 *
 * Also invalidates every publish target: disconnecting an installation deletes the targets that
 * used it, server-side, so a target cached here is stale in a way the user can see.
 */
export function useDisconnectInstallation() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (installationRowId: string) => disconnectInstallation(installationRowId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.installations.all });
      queryClient.invalidateQueries({ queryKey: ["publish-targets"] });
      toast.success("GitHub account disconnected");
    },
    onError: (error) => toast.error(error.message),
  });
}
