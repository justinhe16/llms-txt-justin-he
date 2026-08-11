"use client";

import Image from "next/image";

import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Owner } from "@/lib/api/websites";
import { useUser } from "@/lib/auth/use-user";
import { ownerPill } from "@/lib/crawls/owner";
import { cn } from "@/lib/utils";

interface CrawlOwnerProps {
  /** `WebsiteListItem.user_id` / `WebsiteResponse.user_id` — who added this website. */
  userId: string;
  /** `WebsiteListItem.owner` / `WebsiteResponse.owner` — the same website record's resolved
   * GitHub identity, `null` when the owner has no usable Supabase Auth metadata. */
  owner: Owner | null;
  /** Make the pill a tab stop so its tooltip is keyboard-reachable. `false` in the table:
   * one extra tab stop per row is the cost relative-time.tsx already refuses to pay for
   * the Last-run cell. `true` on the detail header, where the pill is one element on the
   * page and the tab stop is free. */
  focusable?: boolean;
  className?: string;
}

/**
 * The Owner pill. Avatar (or a monogram) plus a real GitHub identity for EVERY owner now —
 * "you" plus your own session's avatar/handle on your own row, `@handle` (falling back to a
 * display name, falling back to a short, honest 8-hex-character id) on everybody else's —
 * with the full `user_id` in a tooltip whenever `text` is not already the unambiguous form.
 *
 * This calls `useUser()` itself rather than taking the signed-in user as a prop. `@supabase/
 * ssr`'s `createBrowserClient` caches a singleton in the browser
 * (node_modules/@supabase/ssr/dist/main/createBrowserClient.js:9-16), so every row's call
 * shares one client and one auth state machine, and `use-user.ts`'s `sameUser` guard returns
 * the previous object identity on `TOKEN_REFRESHED`, so rows do not re-render on the refresh
 * timer. That trade-off holds for the row counts this table renders; a paginated table of
 * hundreds of rows would justify hoisting `useUser()` back up and passing the viewer down.
 * `owner`, by contrast, IS taken as a prop: it comes from the same website record every
 * consumer already has in hand (`crawls-table.tsx`, `website-detail.tsx`), so there is
 * nothing here for a second data source to fetch.
 *
 * Why the column exists at all: ARCHITECTURE.md §4 makes every website readable by every
 * signed-in user, so "whose crawl am I looking at" is a question this table has to answer.
 */
export function CrawlOwner({ userId, owner, focusable = false, className }: CrawlOwnerProps) {
  const { user } = useUser();
  const pill = ownerPill(userId, user, owner);

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          tabIndex={focusable ? 0 : undefined}
          className={cn(
            "inline-flex max-w-full cursor-default rounded-4xl outline-none",
            focusable && "focus-visible:ring-3 focus-visible:ring-ring/50",
            className
          )}
        >
          <Badge
            variant="secondary"
            className={cn(
              "max-w-full gap-1.5 font-normal",
              pill.isYou
                ? "bg-primary/10 text-primary"
                : pill.isIdentified
                  ? "bg-muted text-foreground"
                  : "bg-muted font-mono text-muted-foreground"
            )}
          >
            {pill.avatarUrl !== null ? (
              <Image
                data-icon="inline-start"
                src={pill.avatarUrl}
                alt=""
                width={16}
                height={16}
                className="size-4 shrink-0 rounded-full"
              />
            ) : pill.initial !== null ? (
              <span
                data-icon="inline-start"
                aria-hidden="true"
                className="flex size-4 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[9px] leading-none font-medium"
              >
                {pill.initial}
              </span>
            ) : null}
            {pill.text}
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <span className={pill.tooltipIsRawId ? "font-mono" : undefined}>{pill.tooltip}</span>
      </TooltipContent>
    </Tooltip>
  );
}
