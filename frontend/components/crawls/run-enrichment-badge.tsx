import { CircleCheckIcon, SparklesIcon, type LucideIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { RunListItem } from "@/lib/api/runs";
import {
  ENRICHMENT_APPLIED_BADGE,
  ENRICHMENT_APPLIED_EXPLANATION,
  ENRICHMENT_FALLBACK_BADGE,
  ENRICHMENT_FALLBACK_EXPLANATION,
} from "@/lib/crawls/enrichment-copy";
import { runEnrichmentState, type RunEnrichmentState } from "@/lib/crawls/run-display";

// One `Record` per visual property, the same token-keyed shape `RunStatusIndicator`
// (run-status-indicator.tsx) uses for `RowStatus` — three parallel maps rather than a
// `switch` per property, so adding a third state later is "add one key to each object" and
// a compiler error at every object that was not updated, not a search for every `if` this
// component's logic was spread across.
//
// The icon is the thing worth reading closely: `CircleCheckIcon` for `"applied"` and
// `SparklesIcon` for `"fell_back"` are chosen to be wrong for each other at a glance — a
// filled checkmark reads as "this happened" even before the label is read, and a cluster of
// stars reads as "attempted" rather than "confirmed." That is the point. A reader scanning a
// column of these badges must be able to tell the two apart from their silhouette alone,
// even though the accessible name never depends on the icon doing that work (see the
// component's own docstring below).
const ENRICHMENT_ICON: Record<RunEnrichmentState, LucideIcon> = {
  applied: CircleCheckIcon,
  fell_back: SparklesIcon,
};

const ENRICHMENT_BADGE_TEXT: Record<RunEnrichmentState, string> = {
  applied: ENRICHMENT_APPLIED_BADGE,
  fell_back: ENRICHMENT_FALLBACK_BADGE,
};

const ENRICHMENT_EXPLANATION: Record<RunEnrichmentState, string> = {
  applied: ENRICHMENT_APPLIED_EXPLANATION,
  fell_back: ENRICHMENT_FALLBACK_EXPLANATION,
};

/**
 * The badge that appears beside `RunStatusIndicator` reporting what a run's own `stats` say
 * about enrichment — `lib/crawls/run-display.ts`'s `runEnrichmentState` is the predicate
 * deciding which of three states applies, and this component renders `null` for the third
 * one (nothing to say) so a call site never has to guard the render itself.
 *
 * ## Three states, not two
 *
 * Before this ticket the only state this component knew about was "asked and did not get
 * it" (`runEnrichmentFellBack`). A run that enriched successfully and a run that never asked
 * both rendered nothing, which made them indistinguishable — exactly the gap PER-194 left
 * open. `runEnrichmentState` now also reports `"applied"`, and this component renders a
 * second, positive badge for it, wired to `ENRICHMENT_APPLIED_BADGE` /
 * `ENRICHMENT_APPLIED_EXPLANATION` (lib/crawls/enrichment-copy.ts) rather than the fallback
 * pair. The `null` case — never requested, or a pre-`RUN_STATS_VERSION`-8 run with no
 * enrichment keys at all — still renders nothing, and still means "this run's record makes
 * no claim," not "this run did not enrich."
 *
 * ## One component, not two
 *
 * A sibling `RunEnrichmentAppliedBadge` was the other shape considered. Rejected because the
 * two states share every structural element — a `Badge` inside a `Tooltip`, an
 * `aria-hidden` icon followed by a short visible label, wired to a longer sentence on hover
 * — and a caller (`runs-tab.tsx`, `output-tab.tsx`'s `RunPicker`, in both the trigger button
 * and each dropdown item) needs exactly one of the three states rendered per run with no
 * branching of its own. Two components would have meant two near-identical files and a call
 * site choosing between them by re-deriving the same predicate this one already owns.
 *
 * Mirrors `RunStatusIndicator` (run-status-indicator.tsx) in shape: a small presentational
 * component taking one run and rendering `null` when its predicate says there is nothing to
 * report. Built from `components/ui/badge.tsx` (`variant="outline"`, the same neutral
 * treatment `run.trigger`'s pill uses in `runs-tab.tsx`) inside a `Tooltip` carrying the full
 * sentence — the same disabled-control tooltip idiom `ScheduleControls` (schedule-controls.tsx)
 * uses for its own "why is this inert" explanation, applied here to "why does this row look
 * different." The visible badge text is never the only thing carrying the distinction between
 * states — `ENRICHMENT_BADGE_TEXT` differs by state on its own, so a screen reader announces
 * "Enriched with Claude" or "Enrichment unavailable" without ever depending on the icon that
 * sits beside it (`aria-hidden`), matching the text/aria affordance `RunStatusIndicator`
 * already provides for its own dot-plus-label pair.
 */
export function RunEnrichmentBadge({
  run,
}: {
  run: Pick<RunListItem, "status" | "stats">;
}) {
  const state = runEnrichmentState(run);
  if (state === null) return null;

  const Icon = ENRICHMENT_ICON[state];

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="gap-1 text-muted-foreground">
          <Icon aria-hidden="true" className="size-3" />
          {ENRICHMENT_BADGE_TEXT[state]}
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{ENRICHMENT_EXPLANATION[state]}</TooltipContent>
    </Tooltip>
  );
}
