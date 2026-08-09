"use client";

import { ChevronDownIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { RunDetail, RunListItem } from "@/lib/api/runs";
import { isActiveRunStatus } from "@/lib/api/run-status";
import { useRun } from "@/lib/query/use-run";
import { rowStatusFromRunStatus } from "@/lib/crawls/row-status";
import { selectRunToShow } from "@/lib/crawls/select-run";

import { CrawlProvenance } from "./crawl-provenance";
import { CrawlsError } from "./crawls-error";
import { DownloadFullButton } from "./download-full-button";
import { LlmsTxtViewer } from "./llms-txt-viewer";
import { RelativeTime } from "./relative-time";
import { RunEnrichmentBadge } from "./run-enrichment-badge";
import { RunStatusIndicator } from "./run-status-indicator";

type OutputTabProps = {
  origin: string;
  runs: RunListItem[];
  isLoadingRuns: boolean;
  /** The run-history fetch's own failure, if it failed. Passed in — rather than this tab
   * treating an empty list as "no runs" — because the two are completely different things to
   * a reader: "this site has never been crawled" versus "we could not find out". */
  runsError: Error | null;
  onRetryRuns: () => void;
  isRetryingRuns: boolean;
  /** From `?run=` — `null` when the user has not picked one, in which case
   * `selectRunToShow` falls back to the most recent completed run. */
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
  canRun: boolean;
};

/**
 * Picks which run's `llms.txt` to show, and shows it.
 *
 * ## The default is the most recent *completed* run, not the most recent run
 *
 * The newest run is frequently `pending` — a user clicks "Run now" and lands here — and
 * defaulting to it would replace an artifact they can read with a spinner they cannot. The
 * fallback chain is: the run named in `?run=`, else the newest completed one, else the
 * newest run of any status (so a website whose only run failed shows that failure rather
 * than an empty page).
 *
 * ## One detail request, for one run
 *
 * `GET /runs/{id}` is the only endpoint that returns `llms_txt`, and it is issued from
 * `RunOutput` below — a component that is rendered once, for the selected run. That is the
 * performance trap this ticket calls out by name: the list endpoint omits `llms_txt`
 * precisely so a history of 200 runs does not pull 200 artifacts, and it would be undone by
 * calling `useRun` from inside a `.map` over the rows.
 */
export function OutputTab({
  origin,
  runs,
  isLoadingRuns,
  runsError,
  onRetryRuns,
  isRetryingRuns,
  selectedRunId,
  onSelectRun,
  canRun,
}: OutputTabProps) {
  const runIdToShow = selectRunToShow(runs, selectedRunId);

  if (isLoadingRuns) return <Skeleton className="h-64 w-full rounded-lg" />;

  // Having something to show comes FIRST — before the error branch, not after it.
  //
  // `RunOutput`'s `GET /runs/{id}` is an independent request, so a run named by `?run=` is
  // displayable even when the *list* fetch failed and `runs` is empty. Checking the error
  // first would have made "an explicit `?run=` always wins" (lib/crawls/select-run.ts) true
  // of the selection and false of the rendering, which is the same shape of mismatch — a
  // comment promising more than the code does — that produced the bug that function exists
  // to have fixed.
  if (runIdToShow !== null) {
    return (
      <div className="min-w-0 space-y-4">
        {/* No picker when the list is empty or unreadable: a dropdown of nothing is not a
            control, and the run on screen arrived by id rather than by being chosen. */}
        {runs.length > 0 && (
          <RunPicker runs={runs} selectedRunId={runIdToShow} onSelectRun={onSelectRun} />
        )}
        <RunOutput runId={runIdToShow} origin={origin} />
      </div>
    );
  }

  // Nothing to show, and we know why. Distinguishing this from the empty state below is the
  // whole point: telling someone to "run a crawl" because we could not read their run list
  // is a claim about their data made from a failure to read it.
  if (runsError !== null) {
    return (
      <CrawlsError
        error={runsError}
        onRetry={onRetryRuns}
        isRetrying={isRetryingRuns}
        hasStaleData={false}
      />
    );
  }

  // State 1 of 4: nothing to show, and no error — this site genuinely has no runs.
  return (
    <div className="rounded-lg border border-border bg-card p-10 text-center">
      <p className="text-sm font-medium text-foreground">Run a crawl to generate llms.txt</p>
      <p className="mt-1 text-sm text-muted-foreground">
        {canRun
          ? "Use Run now above. The generated file will appear here."
          : "This site has never been crawled. Its owner can start a run from this page."}
      </p>
    </div>
  );
}

/**
 * The dropdown that switches between runs, labelled by relative time and status — the two
 * things that distinguish one run from another to a person. A run id would be precise and
 * useless.
 *
 * `RunEnrichmentBadge` renders in both places a run appears here — the trigger button
 * (`selected`) and every `DropdownMenuRadioItem` in the list — not only the selected one.
 * Enrichment state is exactly the kind of thing a person uses this picker to look for ("which
 * run actually got Claude's titles?"), so it has to be visible while choosing, not only after.
 * Both call sites hand the component a raw `run` and let `runEnrichmentState` decide what, if
 * anything, to show; neither re-derives that predicate itself.
 */
function RunPicker({
  runs,
  selectedRunId,
  onSelectRun,
}: {
  runs: RunListItem[];
  selectedRunId: string;
  onSelectRun: (runId: string) => void;
}) {
  const selected = runs.find((run) => run.id === selectedRunId);

  return (
    <div className="flex min-w-0 items-center gap-3">
      <span className="shrink-0 text-sm text-muted-foreground">Showing</span>
      <DropdownMenu>
        {/* `shrink min-w-0` overrides the base button's `shrink-0`, and `h-auto` its fixed
            height: together they let the trigger narrow to the space it actually has and wrap
            its own contents onto a second line rather than running off the right edge of a
            375px viewport. The trigger renders one run; the MENU is what needed widening. */}
        <DropdownMenuTrigger asChild>
          <Button variant="outline" className="h-auto min-w-0 shrink justify-between gap-3 py-1.5">
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              {selected ? (
                <>
                  <RelativeTime iso={selected.started_at} className="text-sm" />
                  <RunStatusIndicator status={rowStatusFromRunStatus(selected.status)} />
                  <RunEnrichmentBadge run={selected} />
                </>
              ) : (
                "Selected run"
              )}
            </span>
            <ChevronDownIcon aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        {/* Capped and scrollable: the picker lists every run loaded into the Runs tab,
            which grows with each "Load more" and would otherwise run off the screen.

            `w-auto` OVERRIDES the primitive's default `w-(--radix-dropdown-menu-trigger-width)`
            (components/ui/dropdown-menu.tsx), and the `min-w-*` beside it is what keeps the
            menu at least as wide as the button that opened it. Sizing a menu to its trigger is
            the right default for a select-shaped control whose options are short; it is the
            wrong one here, because a row is a timestamp, a status and possibly an enrichment
            badge, and the trigger renders only ONE row's worth of that while the menu has to
            fit the widest. Left at the default (and at the `w-72` this previously pinned it
            to), "43m ago" wrapped onto two lines and the badge was clipped at the right edge.
            `calc(... + 2.75rem)` is the padding the trigger does not pay for: each item
            reserves `pr-8` for the radio checkmark, on top of the content's own `p-1`. The
            `max-w-*` is the other side of that: `min-w` alone would happily push the menu past
            the right edge of a 375px viewport. It is Radix's own measurement of the room left
            between the trigger and that edge, not a `100vw` guess, so it already accounts for
            where the menu was actually placed. */}
        <DropdownMenuContent className="max-h-80 w-auto max-w-(--radix-dropdown-menu-content-available-width) min-w-[calc(var(--radix-dropdown-menu-trigger-width)+2.75rem)] overflow-y-auto">
          <DropdownMenuRadioGroup value={selectedRunId} onValueChange={onSelectRun}>
            {runs.map((run) => (
              <DropdownMenuRadioItem key={run.id} value={run.id} className="gap-2 py-1.5">
                {/* `whitespace-nowrap` on the row, and a floor under the timestamp: the two
                    together are what make this list scannable, keeping every status dot in one
                    column instead of wherever "43m ago" versus "1d ago" happens to end. */}
                <span className="flex min-w-0 items-center gap-2 whitespace-nowrap">
                  <RelativeTime iso={run.started_at} className="min-w-14 text-sm" />
                  <RunStatusIndicator status={rowStatusFromRunStatus(run.status)} />
                  <RunEnrichmentBadge run={run} />
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/**
 * One run's artifact, plus — for every run past the active/in-flight state — the provenance
 * panel underneath it. The only place `GET /runs/{id}` is called.
 *
 * `useRun` polls this on its own while the run is active (`runIsActive`), so a run watched
 * from `pending` through `processing` to `completed` fills in its viewer without the user
 * doing anything, and the polling stops by itself the moment it lands.
 *
 * `CrawlProvenance` (PER-196) renders for the three TERMINAL branches `RunArtifact` below
 * handles — failed, completed-without-an-artifact, and success — never for `isPending`,
 * `isError`, or the still-active branch: an in-flight run has no stats yet to describe, and a
 * skeleton followed by a provenance panel would claim otherwise. `!isActiveRunStatus(run.status)`
 * is the single predicate for "terminal" here, the same one `RunArtifact`'s own first branch
 * already reads `run.status` against.
 */
function RunOutput({ runId, origin }: { runId: string; origin: string }) {
  const { data: run, isPending, isError, error } = useRun(runId);

  if (isPending) return <Skeleton className="h-64 w-full rounded-lg" />;

  if (isError) {
    return (
      <p className="rounded-lg border border-border bg-card p-6 text-sm text-status-failed">
        {error.message}
      </p>
    );
  }

  return (
    <>
      <RunArtifact run={run} runId={runId} origin={origin} />
      {/* A fragment is correct here — these become direct children of `OutputTab`'s own
          `<div className="min-w-0 space-y-4">`, so its existing spacing applies with no new
          wrapper needed. */}
      {!isActiveRunStatus(run.status) && <CrawlProvenance run={run} />}
    </>
  );
}

/**
 * The artifact viewer for one run, in whichever of its four possible shapes `run.status` (and
 * whether it actually has an `llms_txt`) puts it in.
 */
function RunArtifact({
  run,
  runId,
  origin,
}: {
  run: RunDetail;
  runId: string;
  origin: string;
}) {
  // State 2 of 4: the run is still going. A skeleton plus a live status, rather than an
  // empty viewer — there is genuinely no artifact yet, and saying so is more useful than
  // rendering zero lines as though that were the result.
  //
  // `DownloadFullButton` appears here too, disabled — the control is a fixture of this tab
  // regardless of which of the four states is showing, so it is disabled with a reason
  // rather than left out. A control that appears only once a run succeeds would look, on
  // every run before that, like a bug rather than a state the person is currently in.
  if (isActiveRunStatus(run.status)) {
    return (
      <div className="space-y-3 rounded-lg border border-border bg-card p-6">
        <div className="flex items-center justify-between gap-3 text-sm text-foreground">
          <div className="flex items-center gap-2">
            <RunStatusIndicator status={rowStatusFromRunStatus(run.status)} />
            <span className="text-muted-foreground">
              Generating… this page updates on its own when the crawl finishes.
            </span>
          </div>
          <DownloadFullButton
            runId={runId}
            origin={origin}
            disabledReason="This run hasn't finished yet — its llms-full.txt isn't ready."
          />
        </div>
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }

  // State 3 of 4: the run failed. Show why — an empty viewer for a failed run is the
  // specific outcome this ticket's acceptance criteria rule out.
  if (run.status === "failed") {
    return (
      <div className="space-y-3 rounded-lg border border-border bg-card p-6">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <RunStatusIndicator status="failed" />
            <span className="text-sm text-muted-foreground">
              This run produced no llms.txt.
            </span>
          </div>
          <DownloadFullButton
            runId={runId}
            origin={origin}
            disabledReason="This run failed before it could produce an llms-full.txt."
          />
        </div>
        <pre className="max-h-64 overflow-auto rounded-md bg-status-failed-surface px-4 py-3 font-mono text-xs whitespace-pre-wrap break-words text-status-failed">
          {run.error ?? "The run failed, but recorded no error message."}
        </pre>
      </div>
    );
  }

  // A completed run with no artifact. Not one of the ticket's four states because it should
  // not happen — a run that completes writes its `llms_txt` — but "completed" and "has an
  // artifact" are separate facts in the response, and rendering an empty viewer for this
  // would look identical to a crawl that legitimately found nothing.
  if (run.llms_txt === null || run.llms_txt === undefined) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card p-6">
        <p className="text-sm text-muted-foreground">
          This run completed without storing an llms.txt.
        </p>
        <DownloadFullButton
          runId={runId}
          origin={origin}
          disabledReason="This run completed without storing an llms-full.txt."
        />
      </div>
    );
  }

  // State 4 of 4: success.
  return <LlmsTxtViewer content={run.llms_txt} origin={origin} runId={runId} />;
}
