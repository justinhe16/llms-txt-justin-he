import Link from "next/link";
import {
  ChevronRightIcon,
  DownloadIcon,
  FileCode2Icon,
  ListFilterIcon,
  NetworkIcon,
  type LucideIcon,
} from "lucide-react";

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { RunDetail } from "@/lib/api/runs";
import {
  blockedReasonCopy,
  discoverySourceCopy,
  fetchCapNote,
  PROVENANCE_BUDGET_LABEL,
  PROVENANCE_BUDGET_UNIT,
  PROVENANCE_BYTES_LABEL,
  PROVENANCE_HEADINGS,
  PROVENANCE_PREVIEW,
  PROVENANCE_SEGMENTS,
  PROVENANCE_STAGE_UNITS,
  PROVENANCE_SUMMARY,
  SELECTION_DOCS_LINK,
  SELECTION_STATE_COPY,
  selectionBudgetNote,
  selectionBudgetSpareNote,
  selectionRuleCopy,
  stageUnit,
} from "@/lib/crawls/provenance-copy";
import {
  formatBytes,
  runProvenance,
  selectionSelected,
  type PageBudget,
  type RunProvenance,
  type SelectionState,
} from "@/lib/crawls/run-provenance";
import { cn } from "@/lib/utils";

import { EmptyCell } from "./empty-cell";

/**
 * "Show how this was built" — a collapsible disclosure under the `llms.txt` viewer on the
 * Output tab (PER-196), explaining how this specific run got from its seed URL to its index:
 * which discovery source won, how many candidate URLs it found, how many were dropped and by
 * which rule, how many were selected, and which cap (if any) ended the run.
 *
 * **Counts only. No URL lists.** A deliberate scope decision the ticket states by name, not a
 * first cut — see `lib/crawls/run-provenance.ts`'s own module docstring for the numbers this
 * reads and `lib/crawls/provenance-copy.ts` for every string it renders.
 *
 * ## It is drawn as a funnel, because it is one
 *
 * Four stages on a single vertical rail, each with a proportional bar drawn to ONE shared
 * scale (`funnelScale` below), so the narrowing from "URLs discovery found" to "pages that
 * reached `llms.txt`" is visible before a single number is read. The earlier shape of this
 * panel — four `<dl>`s of label/value pairs stacked with no relationship drawn between them —
 * made every stage look like an independent statistic, which is the one thing they are not.
 *
 * Each bar's segments are `blue = carried forward, grey = lost here, rose = failed`, plus one
 * lighter blue that appears exactly once: the seed page, in the Fetch stage. That segment is
 * the visual half of this module's own arithmetic caveat — a run can fetch MORE pages than
 * ranking selected, because the seed is fetched separately and is never a member of `selected`
 * (`lib/crawls/run-provenance.ts`'s invariants section). A funnel that silently widened at one
 * stage would read as a bug; one that widens by a segment labelled "Seed page" reads as what
 * happened. `blocked` (`RUN_STATS_VERSION` 11) is grey, deliberately grouped with "lost here"
 * rather than given rose alongside `failed` — a page a WAF challenged is not a failure of this
 * crawler's own doing, the same "hitting a cap is a success" colouring `notAttempted` already
 * gets (ARCHITECTURE.md §3.4).
 *
 * The bars themselves are `aria-hidden`: every number in them is also in the legend directly
 * underneath as text, so a screen reader gets the counts once rather than twice, and gets them
 * from the element that actually spells them out.
 *
 * ## Five states, driven by `runProvenance(run)`
 *
 * | `run.stats` | rendered |
 * | --- | --- |
 * | absent entirely | nothing — `CrawlProvenance` returns `null` |
 * | present, `dropped` absent (pre-`RUN_STATS_VERSION`-9) | all four stages; Selection shows the totals it does have and says the per-rule split was not recorded |
 * | present, `dropped` absent AND no totals either | all four stages; Selection says nothing about selection was recorded |
 * | present, `urls_discovered === 0` | all four stages; Discovery and Selection agree on whether the seed itself was crawled, and never claim it was when it was not |
 * | present, a real breakdown | all four stages, the per-rule table under the Selection bar |
 *
 * ## The page budget, and the two sentences that depend on it
 *
 * Selection carries a `Page budget` line in the same slot Fetch puts its byte total, and — on
 * a run where `over_limit` fired — a sentence saying how many URLs were ranked, how many the
 * budget had room for, and that no run gets more. Both come from `provenance.pageBudget`,
 * which is `stats.max_pages` on a `RUN_STATS_VERSION` 10 row and a narrow derivation on an
 * older one (`lib/crawls/run-provenance.ts`'s `PageBudget`); a run whose budget cannot be
 * established renders neither, rather than a guess.
 *
 * The Fetch stage depends on the same signal for the opposite reason. `cap_hit: null` is what
 * a budget-saturated run records — always, on any site larger than the budget — so the stage's
 * closing sentence goes through `fetchCapNote`, not `capHitCopy`, and stops claiming the run
 * "finished before any budget ran out" directly beneath a table saying otherwise.
 *
 * A failed run is not a sixth state of this component — it renders whichever of the four
 * stages its own partial `stats` describe, exactly as the ticket's own States section asks
 * ("show whatever stages completed before the failure"). One place `run.status === "failed"`
 * changes wording directly is the Fetch stage's closing sentence, which reports the run
 * failed rather than naming a cap that never actually ended it — or, when the failure was a
 * detected WAF/CDN block on the run's own SEED (`RUN_STATS_VERSION` 11's `blocked_reason`),
 * names THAT specifically ("This site returned an automated-traffic challenge...") rather than
 * the generic sentence, since the block is exactly why fetching produced nothing.
 *
 * ## Why `<details>`/`<summary>` and not a `components/ui/` primitive
 *
 * ARCHITECTURE.md §8.4 reserves `components/ui/` for shadcn-generator output kept close to
 * what the generator produced, so it can be regenerated — a hand-written disclosure component
 * there is exactly the app-specific logic that section forbids, and pulling in Radix's
 * `Collapsible` would add a client-side dependency, its own state, and its own animation
 * config for this one caller. Every `[A11y]`/`[Default]` requirement this ticket asks for is
 * native instead: `<summary>` is Enter/Space-operable and exposes its own expanded state with
 * no JavaScript, and a `<details>` with no `open` attribute is closed on first paint with no
 * hydration flash. This is the app's first use of `<details>`.
 *
 * No `"use client"`: this subtree needs no interactivity beyond what `<details>` already
 * provides natively, so it is a server-renderable component — `output-tab.tsx` already carries
 * the client boundary above it, so omitting the directive here changes nothing about where
 * this code runs, but it is still the honest signal for what this file itself needs.
 */
export function CrawlProvenance({ run }: { run: Pick<RunDetail, "status" | "stats"> }) {
  const provenance = runProvenance(run);
  if (provenance === null) return null;

  const stages = funnelStages(provenance, run.status);
  const scale = funnelScale(stages);

  return (
    <details className="group rounded-lg border border-border bg-card">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg p-4 text-sm font-medium text-foreground transition-colors hover:bg-accent/40 [&::-webkit-details-marker]:hidden">
        <ChevronRightIcon
          aria-hidden="true"
          className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90"
        />
        <span className="flex-1">{PROVENANCE_SUMMARY}</span>
        {/* The shape of the run, before it is opened — and hidden once it is, because the
            funnel below says the same thing better. `aria-hidden` for the same reason the bars
            are: every number here is repeated as text three lines down. */}
        <span aria-hidden="true" className="group-open:hidden">
          <span className="hidden text-xs font-normal tabular-nums text-muted-foreground sm:inline">
            {funnelPreview(provenance)}
          </span>
        </span>
      </summary>

      <ol className="border-t border-border p-4 sm:p-5">
        {stages.map((stage, index) => (
          <Stage
            key={stage.key}
            stage={stage}
            scale={scale}
            isLast={index === stages.length - 1}
          />
        ))}
      </ol>
    </details>
  );
}

// ---------------------------------------------------------------------------------------
// The funnel's own small vocabulary: one segment of one stage's bar, and one stage.
// ---------------------------------------------------------------------------------------

/** Which fill a segment gets, keyed by what the segment MEANS rather than by which stage it
 * appears in — so "carried forward" is the same blue in all four stages and "lost here" is the
 * same grey, which is the entire reason the bars can be compared to each other at a glance.
 * `--chart-1` is `--primary`; `--chart-3` is two steps lighter in the same hue (app/globals.css).
 * Rose appears exactly once, on `failed`, and nowhere near `notAttempted` — not attempting a
 * page because a cap ended the run first is a success, not a failure (ARCHITECTURE.md §3.4). */
const SEGMENT_FILL = {
  discovered: "bg-chart-1",
  selected: "bg-chart-1",
  dropped: "bg-muted-foreground/25",
  seed: "bg-chart-3",
  frontier: "bg-chart-1",
  failed: "bg-status-failed/45",
  // Neutral, not rose — see the module docstring's "blocked" paragraph: a WAF-challenged page
  // is not a failure this crawler committed, so it shares `dropped`/`notAttempted`'s grey
  // rather than `failed`'s rose.
  blocked: "bg-muted-foreground/25",
  notAttempted: "bg-muted-foreground/25",
  listed: "bg-chart-1",
  omittedEmpty: "bg-muted-foreground/25",
} as const;

type SegmentKey = keyof typeof SEGMENT_FILL;

interface Segment {
  key: SegmentKey;
  value: number;
}

interface FunnelStage {
  key: "discovery" | "selection" | "fetch" | "index";
  heading: React.ReactNode;
  icon: LucideIcon;
  /** The stage's headline count, or `null` when this run's `stats` do not record it — an
   * `EmptyCell`, never a `0` standing in for "unknown". */
  headline: number | null;
  unit: { one: string; other: string };
  /** The line under the heading naming what this stage worked with — the discovery source, or
   * the byte total. `null` for the stages that have nothing to add beyond their numbers. */
  subject: React.ReactNode | null;
  segments: Segment[];
  /** The one plain-language sentence closing the stage. */
  note: string | null;
  /** Anything further this stage renders below its note — today, only Selection's per-rule
   * table. */
  detail: React.ReactNode | null;
}

/** Every segment's value summed — the width one stage's bar occupies before it is scaled
 * against the other three. */
function stageTotal(stage: FunnelStage): number {
  return stage.segments.reduce((sum, segment) => sum + segment.value, 0);
}

/**
 * The denominator all four bars are drawn against: the widest stage.
 *
 * One shared scale is what makes this a funnel rather than four unrelated progress bars — a
 * per-stage scale would draw "1 page listed of 1" and "412 URLs of 412" as the same full-width
 * bar. Floored at 1 so a run where every stage is zero divides by something.
 */
function funnelScale(stages: FunnelStage[]): number {
  return Math.max(1, ...stages.map(stageTotal));
}

/** The `<summary>` line's micro-summary — "2 found · 3 fetched · 1 listed", with any part this
 * run never recorded left out rather than shown as a zero. `blocked` (`RUN_STATS_VERSION` 11)
 * joins the line only when this run actually had one — a blocked SEED means `fetchedTotal` is
 * already `0`, so the preview line for that run reads "0 found · 0 fetched · 1 blocked", never
 * silently omitting the one fact that explains why fetching came up empty. */
function funnelPreview(provenance: RunProvenance): string {
  const parts: string[] = [];
  if (provenance.urlsDiscovered !== null) {
    parts.push(`${provenance.urlsDiscovered.toLocaleString()} ${PROVENANCE_PREVIEW.found}`);
  }
  parts.push(`${fetchedTotal(provenance).toLocaleString()} ${PROVENANCE_PREVIEW.fetched}`);
  if (provenance.fetch.blocked > 0) {
    parts.push(`${provenance.fetch.blocked.toLocaleString()} ${PROVENANCE_PREVIEW.blocked}`);
  }
  if (provenance.index.kind === "stored") {
    parts.push(`${provenance.index.indexed.toLocaleString()} ${PROVENANCE_PREVIEW.listed}`);
  }
  return parts.join(" · ");
}

/** Pages actually fetched, seed included — `FetchInfo` splits the seed out on purpose (see
 * `run-provenance.ts`), and this is the one place the two halves are added back together. */
function fetchedTotal(provenance: RunProvenance): number {
  return provenance.fetch.frontierFetched + (provenance.fetch.seedFetched ? 1 : 0);
}

/**
 * `RunProvenance` turned into the four stages the panel draws, in execution order.
 *
 * Built as data rather than as four bespoke JSX blocks for one reason that is not tidiness:
 * `funnelScale` has to see every stage's segments BEFORE any bar is rendered, because they
 * share a denominator. Four components each computing their own bar could not have one.
 */
function funnelStages(provenance: RunProvenance, status: RunDetail["status"]): FunnelStage[] {
  const { fetch, index, selection } = provenance;
  const discovered = provenance.urlsDiscovered;
  const source = discoverySourceCopy(provenance.discoverySource, fetch.seedFetched);
  const selected = selectionSelected(selection);

  return [
    {
      key: "discovery",
      heading: PROVENANCE_HEADINGS.discovery,
      icon: NetworkIcon,
      headline: discovered,
      unit: PROVENANCE_STAGE_UNITS.discovery,
      subject:
        source === null ? <EmptyCell label="discovery source not recorded for this run" /> : source.label,
      segments: [{ key: "discovered", value: discovered ?? 0 }],
      note: source?.explanation ?? null,
      detail: null,
    },
    {
      key: "selection",
      heading: (
        <Link
          href={SELECTION_DOCS_LINK.href}
          className="underline-offset-4 hover:text-foreground hover:underline"
        >
          {PROVENANCE_HEADINGS.selection}
        </Link>
      ),
      icon: ListFilterIcon,
      headline: selected,
      unit: PROVENANCE_STAGE_UNITS.selection,
      // The same `label + value` slot the Fetch stage puts its byte total in — the two stages
      // that ran against a configured number present it identically, rather than one of them
      // burying it in prose.
      subject:
        provenance.pageBudget === null ? null : (
          <span className="text-muted-foreground">
            {PROVENANCE_BUDGET_LABEL}{" "}
            <span className="tabular-nums text-foreground">
              {provenance.pageBudget.maxPages.toLocaleString()}{" "}
              {stageUnit(PROVENANCE_BUDGET_UNIT, provenance.pageBudget.maxPages)}
            </span>
          </span>
        ),
      segments: selectionSegments(selection),
      note: selectionNote(selection, provenance.pageBudget, provenance.overLimit),
      detail: <SelectionRules selection={selection} />,
    },
    {
      key: "fetch",
      icon: DownloadIcon,
      heading: PROVENANCE_HEADINGS.fetch,
      headline: fetchedTotal(provenance),
      unit: PROVENANCE_STAGE_UNITS.fetch,
      subject: (
        <span className="text-muted-foreground">
          {PROVENANCE_BYTES_LABEL}{" "}
          <span className="tabular-nums text-foreground">
            {fetch.bytesFetched === null ? (
              <EmptyCell label="byte total not recorded for this run" />
            ) : (
              formatBytes(fetch.bytesFetched)
            )}
          </span>
        </span>
      ),
      segments: [
        { key: "seed", value: fetch.seedFetched ? 1 : 0 },
        { key: "frontier", value: fetch.frontierFetched },
        { key: "failed", value: fetch.failed },
        { key: "blocked", value: fetch.blocked },
        { key: "notAttempted", value: fetch.notAttempted },
      ],
      // A failed run names its failure rather than a cap that never actually ended it — a run
      // that died mid-fetch did not "finish before any budget ran out", which is what
      // `capHitCopy(null)` would have claimed. `fetchCapNote` is what rules out the OTHER way
      // that same sentence goes wrong on a COMPLETED run: a run whose page budget was spent at
      // selection also reports `cap_hit: null`, and structurally always will — see that
      // function's own docstring. On a FAILED run, a detected WAF/CDN block on the SEED
      // (`AccessBlockedError`, `internals/crawler.py`) names THAT specifically, ahead of the
      // generic failure sentence — `fetch.blockedReason` is set on a failed run precisely when
      // the block is what ended it, since `_note_block` runs before `seed_error` is even
      // raised.
      note:
        status === "failed"
          ? fetch.blockedReason !== null
            ? blockedReasonCopy(fetch.blockedReason).explanation
            : "This run failed before fetching finished."
          : fetchCapNote(fetch.capHit, provenance.overLimit, fetch.failed),
      // A completed run with one or more blocked FRONTIER pages gets one extra sentence
      // explaining what "Blocked" in the legend means — the failed-run case above already
      // says this as the stage's own `note`, so this only fires when the run did not fail.
      detail:
        status !== "failed" && fetch.blocked > 0 && fetch.blockedReason !== null ? (
          <p className="mt-2 text-xs text-muted-foreground">
            {blockedReasonCopy(fetch.blockedReason).explanation}
          </p>
        ) : null,
    },
    {
      key: "index",
      heading: PROVENANCE_HEADINGS.index,
      icon: FileCode2Icon,
      headline: index.kind === "stored" ? index.indexed : null,
      unit: PROVENANCE_STAGE_UNITS.index,
      subject: null,
      segments:
        index.kind === "stored"
          ? [
              { key: "listed", value: index.indexed },
              { key: "omittedEmpty", value: index.omittedEmpty },
            ]
          : [],
      note: index.kind === "stored" ? null : "No index was stored for this run.",
      detail: null,
    },
  ];
}

/** The Selection bar: kept beside lost, in every state that knows both numbers. The two states
 * that know neither (`"unavailable"`) or that have nothing to split (`"seed_only"`,
 * `"seed_not_fetched"` — discovery found nothing to select FROM) render an empty track under
 * the sentence that explains why, rather than a zero-width bar pretending to be data. */
function selectionSegments(selection: SelectionState): Segment[] {
  switch (selection.kind) {
    case "unavailable":
    case "seed_not_fetched":
    case "seed_only":
      return [];
    case "totals_only":
    case "breakdown":
      return [
        { key: "selected", value: selection.selected },
        { key: "dropped", value: selection.droppedTotal },
      ];
  }
}

/**
 * The Selection stage's sentence.
 *
 * The `"breakdown"` arm is where PER-201 lands, and the ordering inside it is the point: the
 * budget sentence outranks `nothingDropped` and outranks rendering nothing at all, because on
 * a run where `over_limit` fired it is the only line that answers the question the table
 * directly above it provokes — whether those URLs failed something, and whether more could
 * have got through. The per-rule table says WHAT dropped them; this says the cut was a
 * position in a queue rather than a bar to clear, and that no run clears it.
 */
function selectionNote(
  selection: SelectionState,
  budget: PageBudget | null,
  overLimit: number | null
): string | null {
  switch (selection.kind) {
    case "unavailable":
      return SELECTION_STATE_COPY.unavailable;
    case "seed_not_fetched":
      return SELECTION_STATE_COPY.seedNotFetched;
    case "seed_only":
      return SELECTION_STATE_COPY.seedOnly;
    case "totals_only":
      return SELECTION_STATE_COPY.totalsOnly;
    case "breakdown":
      if (budget !== null && overLimit !== null && overLimit > 0) {
        return selectionBudgetNote(selection.selected + overLimit, budget);
      }
      // A real breakdown with nothing in it is not an empty table — it is a run where ranking
      // kept everything discovery found, which is a result worth stating in words. With a
      // recorded budget beside it, the stronger version of that statement is available: not
      // just that nothing was dropped, but that there was room to spare.
      if (selection.rows.length === 0) {
        return budget === null
          ? SELECTION_STATE_COPY.nothingDropped
          : `${SELECTION_STATE_COPY.nothingDropped} ${selectionBudgetSpareNote(budget)}`;
      }
      // Rules fired, but not the budget — the site is smaller than the budget and what dropped
      // it was the rules themselves. Saying so keeps a reader from assuming the missing URLs
      // are the ones that lost a ranking they never had to enter.
      return budget !== null && overLimit === 0 ? selectionBudgetSpareNote(budget) : null;
  }
}

// ---------------------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------------------

/**
 * One stage: an icon on the rail, its heading and headline number, its bar, its legend, its
 * sentence, and whatever detail it carries.
 *
 * The rail is the vertical line between icons — `flex-1` inside the grid's first column, which
 * stretches to the row's height, so a stage with a long table under it gets a long connector
 * with no measurement anywhere. `<li>` in an `<ol>`: these are four ordered steps of one
 * process, and that ordering is information a screen reader should get from the markup rather
 * than from the icons it cannot see.
 */
function Stage({
  stage,
  scale,
  isLast,
}: {
  stage: FunnelStage;
  scale: number;
  isLast: boolean;
}) {
  const Icon = stage.icon;

  return (
    <li className="grid grid-cols-[1.75rem_minmax(0,1fr)] gap-x-3 sm:grid-cols-[2rem_minmax(0,1fr)] sm:gap-x-4">
      <div className="flex flex-col items-center">
        <span className="flex size-7 items-center justify-center rounded-full bg-primary/8 text-primary ring-1 ring-primary/15 ring-inset sm:size-8">
          <Icon aria-hidden="true" className="size-3.5 sm:size-4" />
        </span>
        {!isLast && (
          <span
            aria-hidden="true"
            className="mt-1.5 w-px flex-1 bg-gradient-to-b from-border via-border to-border/30"
          />
        )}
      </div>

      <div className={cn("min-w-0", !isLast && "pb-7")}>
        <div className="flex flex-wrap items-baseline justify-between gap-x-3">
          <h4 className="text-xs font-semibold tracking-wider text-muted-foreground uppercase">
            {stage.heading}
          </h4>
          <p className="text-sm text-muted-foreground">
            <span className="text-base font-semibold tabular-nums text-foreground">
              {stage.headline === null ? (
                <EmptyCell label="not recorded for this run" />
              ) : (
                stage.headline.toLocaleString()
              )}
            </span>{" "}
            {stageUnit(stage.unit, stage.headline)}
          </p>
        </div>

        {stage.subject !== null && (
          <p className="mt-1 text-sm text-foreground">{stage.subject}</p>
        )}

        <FunnelBar segments={stage.segments} scale={scale} />
        <SegmentLegend segments={stage.segments} headline={stage.headline} />

        {stage.note !== null && (
          <p className="mt-2 text-xs text-muted-foreground">{stage.note}</p>
        )}
        {stage.detail}
      </div>
    </li>
  );
}

/** One stage's bar, drawn against the funnel's shared scale. `aria-hidden` — `SegmentLegend`
 * directly below carries every number in it as text. */
function FunnelBar({ segments, scale }: { segments: Segment[]; scale: number }) {
  return (
    <div
      aria-hidden="true"
      className="mt-2.5 flex h-2 w-full overflow-hidden rounded-full bg-muted"
    >
      {segments
        .filter((segment) => segment.value > 0)
        .map((segment) => (
          <div
            key={segment.key}
            className={SEGMENT_FILL[segment.key]}
            style={{ width: `${(segment.value / scale) * 100}%` }}
          />
        ))}
    </div>
  );
}

/**
 * The counts under a bar, one entry per segment, each with the swatch that ties it to its band.
 *
 * Zero-valued segments are dropped, so a legend never lists outcomes that did not occur — and
 * a legend left with ONE segment is dropped entirely IF that segment is the number the stage's
 * headline already reported. "412 URLs found" directly above "Discovered 412" is the same fact
 * twice, and on a clean run — nothing dropped, nothing failed, nothing omitted — that is every
 * stage.
 *
 * The `headline` comparison rather than a bare `length < 2` is what keeps the one case where a
 * single segment is worth saying out loud: a failed run's Fetch stage, whose headline is "0
 * pages fetched" over a bar that is entirely the `failed` band. Suppressing that legend would
 * leave a full-width rose bar on screen with nothing anywhere naming it.
 */
function SegmentLegend({
  segments,
  headline,
}: {
  segments: Segment[];
  headline: number | null;
}) {
  const shown = segments.filter((segment) => segment.value > 0);
  if (shown.length === 0) return null;
  if (shown.length === 1 && shown[0].value === headline) return null;

  return (
    <ul className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
      {shown.map((segment) => (
        <li key={segment.key} className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={cn("size-2 shrink-0 rounded-full", SEGMENT_FILL[segment.key])}
          />
          <span className="text-muted-foreground">{PROVENANCE_SEGMENTS[segment.key]}</span>
          <span className="font-medium tabular-nums text-foreground">
            {segment.value.toLocaleString()}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The per-rule breakdown under the Selection bar — which rules did the dropping, and how much
 * each one dropped.
 *
 * Two columns, deliberately: the label/explanation pair is what makes each rule concrete with
 * no example URLs, and a third column for the explanation would break at 375px.
 *
 * `"totals_only"` renders here too, with the one rule that survives on a version-6-to-8 row
 * (`urls_robots_disallowed`) — the alternative was to render nothing and let one sentence carry
 * a stage that still has a real number in it. Everything else it dropped goes under a single
 * unnamed remainder row, which is exactly as much as the row records.
 */
function SelectionRules({ selection }: { selection: SelectionState }) {
  const rows =
    selection.kind === "breakdown"
      ? selection.rows.map((row) => ({ ...selectionRuleCopy(row.key), count: row.count }))
      : selection.kind === "totals_only"
        ? totalsOnlyRules(selection)
        : [];

  if (rows.length === 0) return null;

  return (
    <Table className="mt-3">
      <TableCaption className="sr-only">
        Selection rules, in the order they were checked, and how many candidate URLs each one
        dropped
      </TableCaption>
      <TableHeader>
        <TableRow>
          <TableHead>Rule</TableHead>
          <TableHead className="text-right">URLs dropped</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.label}>
            <TableHead scope="row" className="h-auto py-2 align-top whitespace-normal">
              <span className="block font-medium text-foreground">{row.label}</span>
              {row.explanation !== null && (
                <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                  {row.explanation}
                </span>
              )}
            </TableHead>
            <TableCell className="text-right align-top tabular-nums">
              -{row.count.toLocaleString()}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

/** The most a version-6-to-8 row can say about which rules fired: the robots.txt count it
 * recorded by name, and everything else as one unnamed remainder. Empty when neither is
 * nonzero — a run that dropped nothing has no rules to list, and the stage's own
 * `nothingDropped`-shaped sentence has already said so. */
function totalsOnlyRules(
  selection: Extract<SelectionState, { kind: "totals_only" }>
): { label: string; explanation: string | null; count: number }[] {
  const robots = selection.robotsDisallowed ?? 0;
  const remainder = Math.max(0, selection.droppedTotal - robots);

  return [
    ...(robots > 0 ? [{ ...selectionRuleCopy("robots_disallowed"), count: robots }] : []),
    ...(remainder > 0
      ? [
          {
            label: "Rule not recorded",
            explanation:
              "Dropped by one of the twelve selection rules — this run predates the per-rule counters, so which one is not on the record.",
            count: remainder,
          },
        ]
      : []),
  ];
}
