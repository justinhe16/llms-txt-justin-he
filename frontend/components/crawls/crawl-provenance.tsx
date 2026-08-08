import Link from "next/link";
import { ChevronRightIcon } from "lucide-react";

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { RunDetail } from "@/lib/api/runs";
import {
  CAP_HIT,
  DISCOVERY_SOURCE,
  NO_CAP_HIT,
  PROVENANCE_HEADINGS,
  PROVENANCE_SUMMARY,
  SELECTION_DOCS_LINK,
  selectionRuleCopy,
} from "@/lib/crawls/provenance-copy";
import {
  formatBytes,
  runProvenance,
  type FetchInfo,
  type RunProvenance,
  type SelectionState,
} from "@/lib/crawls/run-provenance";

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
 * ## Four states, driven by `runProvenance(run)`
 *
 * | `run.stats` | rendered |
 * | --- | --- |
 * | absent entirely | nothing — `CrawlProvenance` returns `null` |
 * | present, `dropped` absent (pre-`RUN_STATS_VERSION`-9) | Discovery/Fetch/Index render; Selection says the breakdown isn't available |
 * | present, `urls_discovered === 0` | Discovery/Fetch/Index render; Selection is one sentence, not an empty table |
 * | present, a real breakdown | all four stages, the funnel as a table |
 *
 * A failed run is not a fifth state of this component — it renders whichever of the four
 * stages its own partial `stats` describe, exactly as the ticket's own States section asks
 * ("show whatever stages completed before the failure"). The one place `run.status ===
 * "failed"` changes wording is the Fetch stage's closing sentence, which reports the run
 * failed rather than naming a cap that never actually ended it.
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

  return (
    <details className="group rounded-lg border border-border bg-card">
      <summary
        className="flex cursor-pointer list-none items-center gap-2 p-4 text-sm font-medium text-foreground [&::-webkit-details-marker]:hidden"
      >
        <ChevronRightIcon
          aria-hidden="true"
          className="size-4 text-muted-foreground transition-transform group-open:rotate-90"
        />
        {PROVENANCE_SUMMARY}
      </summary>

      <div className="space-y-6 border-t border-border p-4">
        <DiscoverySection provenance={provenance} />
        <SelectionSection provenance={provenance} />
        <FetchSection fetch={provenance.fetch} status={run.status} />
        <IndexSection provenance={provenance} />
      </div>
    </details>
  );
}

/** One `<dl>` row: a muted label beside a value, the pattern every stage below repeats. */
function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

function StageHeading({ children }: { children: React.ReactNode }) {
  return <h4 className="text-sm font-medium text-foreground">{children}</h4>;
}

function DiscoverySection({ provenance }: { provenance: RunProvenance }) {
  const source = provenance.discoverySource;
  const copy = source === null ? null : (DISCOVERY_SOURCE[source] ?? null);

  return (
    <section>
      <StageHeading>{PROVENANCE_HEADINGS.discovery}</StageHeading>
      <dl className="mt-2 space-y-1 text-sm">
        <Row
          label="Source"
          value={copy === null ? <EmptyCell label="not recorded for this run" /> : copy.label}
        />
        <Row
          label="URLs discovered"
          value={
            provenance.urlsDiscovered === null ? (
              <EmptyCell label="not recorded for this run" />
            ) : (
              provenance.urlsDiscovered.toLocaleString()
            )
          }
        />
      </dl>
      {copy !== null && <p className="mt-1 text-xs text-muted-foreground">{copy.explanation}</p>}
    </section>
  );
}

function SelectionSection({ provenance }: { provenance: RunProvenance }) {
  return (
    <section>
      <StageHeading>
        <Link
          href={SELECTION_DOCS_LINK.href}
          className="text-primary underline-offset-4 hover:underline"
        >
          {PROVENANCE_HEADINGS.selection}
        </Link>
      </StageHeading>
      <div className="mt-2">
        <SelectionBody
          selection={provenance.selection}
          urlsDiscovered={provenance.urlsDiscovered}
        />
      </div>
    </section>
  );
}

function SelectionBody({
  selection,
  urlsDiscovered,
}: {
  selection: SelectionState;
  urlsDiscovered: number | null;
}) {
  switch (selection.kind) {
    case "unavailable":
      return (
        <p className="text-sm text-muted-foreground">
          The selection breakdown isn&apos;t available for this run.
        </p>
      );

    case "seed_only":
      return (
        <p className="text-sm text-muted-foreground">
          Discovery found nothing for this run — it crawled the seed page alone.
        </p>
      );

    case "breakdown":
      return (
        <div className="space-y-2">
          <p className="text-sm text-foreground">
            <span className="tabular-nums font-medium">
              {urlsDiscovered === null ? "—" : urlsDiscovered.toLocaleString()}
            </span>{" "}
            discovered
          </p>

          {/* Two columns, deliberately — the label/explanation pair is what makes each rule
              concrete with no example URLs, and a third column for it would break at 375px. */}
          <Table>
            <TableCaption className="sr-only">
              Selection rules, in the order they were checked, and how many candidate URLs each
              one dropped
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Rule</TableHead>
                <TableHead className="text-right">URLs dropped</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {selection.rows.map((row) => {
                const copy = selectionRuleCopy(row.key);
                return (
                  <TableRow key={row.key}>
                    <TableHead scope="row" className="h-auto py-2 align-top whitespace-normal">
                      <span className="block font-medium text-foreground">{copy.label}</span>
                      {copy.explanation !== null && (
                        <span className="mt-0.5 block text-xs font-normal text-muted-foreground">
                          {copy.explanation}
                        </span>
                      )}
                    </TableHead>
                    <TableCell className="text-right align-top tabular-nums">
                      -{row.count.toLocaleString()}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
            <TableFooter>
              <TableRow>
                <TableCell className="font-medium text-foreground">Selected</TableCell>
                <TableCell className="text-right font-medium tabular-nums text-foreground">
                  {selection.selected.toLocaleString()}
                </TableCell>
              </TableRow>
            </TableFooter>
          </Table>
        </div>
      );
  }
}

function FetchSection({
  fetch,
  status,
}: {
  fetch: FetchInfo;
  status: RunDetail["status"];
}) {
  const totalFetched = fetch.frontierFetched + (fetch.seedFetched ? 1 : 0);

  return (
    <section>
      <StageHeading>{PROVENANCE_HEADINGS.fetch}</StageHeading>
      <dl className="mt-2 space-y-1 text-sm">
        <Row
          label="Fetched"
          value={
            <>
              {totalFetched.toLocaleString()}
              {fetch.seedFetched && (
                <span className="ml-1 text-xs text-muted-foreground">(including the seed)</span>
              )}
            </>
          }
        />
        <Row label="Failed" value={fetch.failed.toLocaleString()} />
        {fetch.notAttempted > 0 && (
          <Row label="Not attempted" value={fetch.notAttempted.toLocaleString()} />
        )}
        <Row
          label="Bytes"
          value={
            fetch.bytesFetched === null ? (
              <EmptyCell label="not recorded for this run" />
            ) : (
              formatBytes(fetch.bytesFetched)
            )
          }
        />
      </dl>
      <p className="mt-2 text-xs text-muted-foreground">
        {status === "failed"
          ? "This run failed before fetching finished."
          : (fetch.capHit === null ? NO_CAP_HIT : (CAP_HIT[fetch.capHit] ?? NO_CAP_HIT))}
      </p>
    </section>
  );
}

function IndexSection({ provenance }: { provenance: RunProvenance }) {
  const { index } = provenance;

  return (
    <section>
      <StageHeading>{PROVENANCE_HEADINGS.index}</StageHeading>
      {index.kind === "not_stored" ? (
        <p className="mt-2 text-sm text-muted-foreground">No index was stored for this run.</p>
      ) : (
        <dl className="mt-2 space-y-1 text-sm">
          <Row label="Pages in llms.txt" value={index.indexed.toLocaleString()} />
          <Row label="Omitted as empty" value={index.omittedEmpty.toLocaleString()} />
        </dl>
      )}
    </section>
  );
}
