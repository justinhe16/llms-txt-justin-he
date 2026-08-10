"use client";

import { useCallback, useEffect, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { toast } from "sonner";

import {
  GITHUB_RESULT_PARAM,
  isGithubCallbackResult,
  type GithubCallbackResult,
} from "@/lib/crawls/github-callback";
import { GITHUB_CONNECT_COPY } from "@/lib/crawls/publish-copy";

/**
 * Turns a `?github=` outcome into a toast. Two exports, not two files, so the effect that fires
 * it is written once — `GithubConnectToast` is the dumb half `WebsiteDetail` renders, and
 * `CrawlsGithubConnectToast` below is the version `/crawls` renders, wired to its own query
 * string because that page has no `useDetailView` to route through.
 *
 * `GithubConnectToast` takes an already-validated result and a callback to clear it, and has no
 * opinion of its own about the query string: `WebsiteDetail` supplies both from `useDetailView`,
 * which is `/crawls/[websiteId]`'s one reader and one writer of the search params (see that
 * hook's own docstring for why a second reader there is forbidden). Do not add a `<Toaster>`
 * here — one is already mounted in `app/layout.tsx`, and a second would double every toast this
 * app ever shows, not just this feature's.
 */
export function GithubConnectToast({
  result,
  onShown,
}: {
  result: GithubCallbackResult | null;
  onShown: () => void;
}) {
  // Guards against toasting the same value twice: React's strict mode double-invokes effects in
  // development, and this ref makes the second invocation for a given value a no-op — the same
  // dedupe `AuthErrorToast` uses for `?auth_error=`, copied rather than reinvented.
  const shownFor = useRef<GithubCallbackResult | null>(null);

  useEffect(() => {
    if (result === null || shownFor.current === result) return;
    shownFor.current = result;

    const copy = GITHUB_CONNECT_COPY[result];
    const show =
      copy.variant === "success" ? toast.success : copy.variant === "info" ? toast.info : toast.error;
    show(copy.title, { description: copy.description });

    onShown();
    // `onShown` sits in this dependency array with no `eslint-disable` and no ref indirection.
    // On the detail page, `onShown` is `useDetailView`'s `clearGithubResult`, which is rebuilt
    // (new identity) once the clear it just performed lands and `searchParams` changes under
    // it — so this effect DOES rerun after every successful clear. That rerun is harmless: by
    // then `result` is `null`, because the query string no longer has `?github=`, and the guard
    // on the line above takes the early return. Two different reasons can rerun this effect
    // (strict-mode's double invocation, and this identity change), so it carries two different
    // guards — the `shownFor` ref for the first, `result === null` for the second — rather than
    // one doing double duty.
  }, [result, onShown]);

  return null;
}

/**
 * The `/crawls` version of the toast above, reading and clearing `?github=` itself.
 *
 * `WebsiteDetail` routes its clear through `useDetailView` because that page already has one
 * query-string writer and a second would race it. `/crawls` has no such hook — at the time this
 * was written, `grep -rn "useSearchParams\|router.replace" app/crawls/ components/crawls/`
 * turned up nothing outside this file — so this component is that page's ONLY reader and writer
 * of the query string, which is what makes a local `useSearchParams` here legitimate rather than
 * a second one competing with a first.
 *
 * The clear below is the PRESERVING form: `URLSearchParams(searchParams.toString())`, then
 * `delete`, then `replace` with whatever is left. `AuthErrorToast`'s `router.replace(pathname)`
 * is deliberately not copied here, even though it looks like the same job — that form is
 * correct on `/`, where `?auth_error=` is the only parameter the landing page ever carries, but
 * `/crawls` is not that page: it is one query-string addition away from `router.replace(pathname)`
 * silently deleting whatever else a future feature puts there, which is the exact bug this
 * ticket exists to fix, replanted in a new file.
 */
export function CrawlsGithubConnectToast() {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();

  const raw = searchParams.get(GITHUB_RESULT_PARAM);
  const result = isGithubCallbackResult(raw) ? raw : null;

  const clear = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete(GITHUB_RESULT_PARAM);
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }, [pathname, router, searchParams]);

  return <GithubConnectToast result={result} onShown={clear} />;
}
