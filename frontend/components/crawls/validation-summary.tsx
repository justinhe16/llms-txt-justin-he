"use client";

import { useId, useState } from "react";
import { AlertTriangleIcon, CheckIcon, ChevronDownIcon, XCircleIcon } from "lucide-react";

import type { RunValidation } from "@/lib/crawls/validation";
import { validationLabel } from "@/lib/crawls/validation";
import { cn } from "@/lib/utils";

/**
 * The conformance strip under the artifact's header: whether this run's `llms.txt` conforms to
 * llmstxt.org, and — expanded — every finding behind that verdict.
 *
 * ## Why the artifact says this about itself, rather than a fifth tab
 *
 * The verdict belongs to ONE run's ONE document, which is exactly what the Output tab is
 * already showing. A separate "Insights" tab would have to re-answer "which run?" with its own
 * picker, and would put the judgement a screen away from the text being judged — where a
 * reader cannot check line 7 against the finding that names line 7. It sits directly above the
 * viewer for the same reason `CrawlProvenance` sits directly below it.
 *
 * This is also deliberately NOT the Trends tab's territory. Trends answers "how has this site
 * changed over time"; this answers "is the document on screen well-formed". Putting a
 * conformance verdict in a time series would invite a sparkline of a boolean.
 *
 * ## Collapsed by default, and silent when there is nothing to say
 *
 * The common case is a clean artifact, where the findings list is empty and the strip is one
 * line of reassurance. Findings open on demand rather than pushing the artifact down the page
 * on every visit. `runValidation` returning `null` — a run from before this check shipped, or
 * one that produced no index — renders nothing at all: see its docstring for why "not checked"
 * is not worth a badge.
 *
 * ## The three states are three colours, and `warnings` is the interesting one
 *
 * llmstxt.org requires only an H1, so "conforms" and "good" are different claims
 * (`lib/crawls/validation.ts`). An amber strip reading "Valid llms.txt · 2 notes" says both at
 * once: the spec is met, and there is something to improve. Collapsing that into a green tick
 * would hide the actionable half; colouring it red would misreport a conformant file.
 */
export function ValidationSummary({ validation }: { validation: RunValidation }) {
  const [isOpen, setIsOpen] = useState(false);
  const panelId = useId();

  const hasFindings = validation.findings.length > 0;
  const { icon: Icon, tone, surface } = VERDICT_STYLE[validation.verdict];

  return (
    <div className={cn("border-b border-border", surface)}>
      <div className="flex items-center justify-between gap-3 px-4 py-2">
        <p className={cn("flex items-center gap-2 text-xs font-medium", tone)}>
          <Icon aria-hidden className="size-3.5 shrink-0" />
          {validationLabel(validation)}
        </p>

        {/* No toggle on a clean artifact: an expander over an empty list is a control that
            does nothing, which is worse than no control. */}
        {hasFindings && (
          <button
            type="button"
            onClick={() => setIsOpen((open) => !open)}
            aria-expanded={isOpen}
            aria-controls={panelId}
            className="flex shrink-0 items-center gap-1 rounded-sm text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
          >
            {isOpen ? "Hide details" : "Show details"}
            <ChevronDownIcon
              aria-hidden
              className={cn("size-3.5 transition-transform", isOpen && "rotate-180")}
            />
          </button>
        )}
      </div>

      {hasFindings && isOpen && (
        <ul id={panelId} className="space-y-2 border-t border-border/60 px-4 py-3">
          {validation.findings.map((finding, index) => (
            // `code` is not unique — one malformed convention repeated across a section
            // produces the same code on many lines — so the key pairs it with the index. The
            // list is never reordered or filtered, so the index is stable for its lifetime.
            <li key={`${finding.code}-${index}`} className="flex items-start gap-2 text-xs">
              <span
                className={cn(
                  "mt-px shrink-0 rounded-sm px-1.5 py-0.5 font-medium",
                  finding.severity === "error"
                    ? "bg-status-failed-surface text-status-failed"
                    : "bg-status-processing-surface text-status-processing"
                )}
              >
                {finding.severity === "error" ? "Error" : "Note"}
              </span>
              {/* The line number is a plain label, not a link: the viewer is a single `<pre>`
                  text node by design (see `LlmsTxtViewer`'s own docstring on why), so there is
                  no per-line element to anchor to. A reader scrolls to it. */}
              {finding.line !== null && (
                <span className="mt-0.5 shrink-0 font-mono text-muted-foreground tabular-nums">
                  L{finding.line}
                </span>
              )}
              <span className="mt-0.5 min-w-0 text-muted-foreground">{finding.message}</span>
            </li>
          ))}

          {/* Said out loud rather than left as a silently short list — the counts in the strip
              above are exact, so a list that stops at 25 of 60 would otherwise contradict them. */}
          {validation.truncated && (
            <li className="text-xs text-muted-foreground italic">
              Showing the first {validation.findings.length} of{" "}
              {validation.errorCount + validation.warningCount} findings.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}

/**
 * Icon, text colour and background per verdict.
 *
 * Every colour here is a `status-*` token, never a raw `emerald-*`/`amber-*`/`rose-*` utility —
 * `app/globals.css` states that rule for exactly this kind of component. The mapping reuses the
 * run-status palette on purpose: a green tick already means "this went well" everywhere else in
 * this app, and a conformance verdict is not the place to teach a second colour language.
 */
const VERDICT_STYLE = {
  clean: {
    icon: CheckIcon,
    tone: "text-status-completed",
    surface: "bg-status-completed-surface/60",
  },
  warnings: {
    icon: AlertTriangleIcon,
    tone: "text-status-processing",
    surface: "bg-status-processing-surface/60",
  },
  errors: {
    icon: XCircleIcon,
    tone: "text-status-failed",
    surface: "bg-status-failed-surface/60",
  },
} as const;
