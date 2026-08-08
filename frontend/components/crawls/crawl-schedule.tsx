"use client";

import { WandIcon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { WebsiteListItem } from "@/lib/api/websites";
import { ENRICHMENT_NEXT_RUN_BADGE, ENRICHMENT_NEXT_RUN_EXPLANATION } from "@/lib/crawls/enrichment-copy";
import { formatAbsoluteTime } from "@/lib/crawls/relative-time";
import { scheduleLabel } from "@/lib/crawls/row-status";

import { EmptyCell } from "./empty-cell";

/**
 * The Schedule column: an interval badge when the website crawls itself on a timer, an em
 * dash when it does not, and — independently of either — a second badge forecasting whether
 * the *next* run will ask Claude to summarize this site's pages.
 *
 * A schedule that exists but is switched off renders as no schedule here (see
 * `scheduleLabel`) — from this table's point of view the row is not going to run on its own
 * either way, and "paused" versus "never configured" is a distinction the detail page can
 * afford to make and a six-column overview cannot.
 *
 * When the backend knows when the next run is due, that goes in a tooltip rather than a
 * second line: it is the natural follow-up question to "daily", and it is not worth a column
 * of its own.
 *
 * ## The enrichment badge renders with no schedule too
 *
 * `website.enrich_with_llm` (`WebsiteListItem`, PER-194) is intent for the site's *next* run,
 * full stop — it says nothing about whether that run is scheduled or manual. A site with no
 * schedule, or a schedule switched off, still runs on demand from "Run now" and that run
 * still spends money on Claude if the toggle is on. If the enrichment badge only appeared
 * alongside the interval badge, the one column that gives a reader a cost signal would have a
 * hole exactly where an unscheduled-but-enriching site sits — so this cell renders the
 * enrichment badge in the `label === null` branch exactly as it does in the scheduled one,
 * beside `EmptyCell` rather than instead of it. Both signals are independent facts about the
 * same website and neither implies the other.
 *
 * This is deliberately the **future-tense** half of enrichment visibility — "what will the
 * next run do" — and reads nothing from a run's own `stats`. The Runs and Output tabs'
 * `RunEnrichmentBadge` (run-enrichment-badge.tsx) is the **past-tense** half — "what did this
 * run actually do" — and the two badges never share wording (`lib/crawls/enrichment-copy.ts`)
 * precisely so a reader who just flipped the toggle on sees this cell change immediately while
 * every run before that moment keeps showing nothing, and reads that gap as history rather
 * than as a bug.
 */
export function CrawlSchedule({ website }: { website: WebsiteListItem }) {
  const label = scheduleLabel(website);
  const enrichmentBadge = website.enrich_with_llm ? <EnrichmentForecastBadge /> : null;

  if (label === null) {
    return (
      <span className="inline-flex items-center gap-2">
        <EmptyCell label="no schedule" />
        {enrichmentBadge}
      </span>
    );
  }

  const nextRunAt = website.schedule?.next_run_at ?? null;
  const intervalBadge = (
    <Badge variant="outline" className="font-normal text-muted-foreground">
      {label}
    </Badge>
  );

  const intervalContent =
    nextRunAt === null ? (
      intervalBadge
    ) : (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex cursor-default">{intervalBadge}</span>
        </TooltipTrigger>
        <TooltipContent>Next run {formatAbsoluteTime(nextRunAt)}</TooltipContent>
      </Tooltip>
    );

  return (
    <span className="inline-flex items-center gap-2">
      {intervalContent}
      {enrichmentBadge}
    </span>
  );
}

/**
 * The forecast half of enrichment visibility — "the next run will summarize with Claude" —
 * shown whenever `website.enrich_with_llm` is `true`, regardless of whether this row also has
 * a schedule.
 *
 * `WandIcon`, not `SparklesIcon`: `RunEnrichmentBadge`'s two badges already own `SparklesIcon`
 * (the fallback) and `CircleCheckIcon` (the positive "applied" state) on the Runs and Output
 * tabs, and this badge answers a different question (intent, not outcome) on a different
 * screen — a plain wand, no stars, keeps this from reading as a restatement of either of
 * those two once a reader has seen both screens in one session.
 *
 * **The label is screen-reader-only, and that is what keeps this cell on one line.** At the
 * 13% `columns.ts` gives the Schedule column, an interval badge plus a badge reading
 * "Enriches next run" does not fit on one line at ordinary desktop widths — the pair wrapped,
 * and a wrapped row is taller than the `ROW_HEIGHT` every row (real and skeleton) is supposed
 * to share, which is the content-jump `columns.ts` exists to prevent. Shrinking the visible
 * mark to the icon alone fixes that at the source rather than letting the row grow.
 *
 * The icon is therefore `aria-hidden` and paired with an `sr-only` label, exactly the split
 * `EmptyCell` (empty-cell.tsx) uses for its em dash: the glyph carries the meaning for sighted
 * readers, the hidden text carries it for everyone else, and the tooltip spells out the full
 * sentence for anyone who hovers. That keeps the affordance off the colour-and-icon-alone
 * footing an unlabelled mark would have.
 */
function EnrichmentForecastBadge() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="outline" className="px-1.5 font-normal text-muted-foreground">
          <WandIcon aria-hidden="true" className="size-3" />
          <span className="sr-only">{ENRICHMENT_NEXT_RUN_BADGE}</span>
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{ENRICHMENT_NEXT_RUN_EXPLANATION}</TooltipContent>
    </Tooltip>
  );
}
