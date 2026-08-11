"use client";

import Image from "next/image";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { signOut } from "@/lib/auth/actions";
import { initials } from "@/lib/auth/initials";
import { useUser } from "@/lib/auth/use-user";
import { createClient } from "@/lib/supabase/client";

async function handleSignOut() {
  // Clear the browser client's own session first: this emits SIGNED_OUT
  // through onAuthStateChange, so useUser() (and this menu) updates
  // immediately without waiting on a navigation. Then call the server
  // action, which clears the cookies Next.js reads on the server and
  // redirects.
  await createClient().auth.signOut();
  await signOut();
}

export function UserMenu() {
  const { user, isLoading } = useUser();

  if (isLoading) {
    return <Skeleton className="size-8 rounded-full" />;
  }

  if (!user) {
    return null;
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="flex items-center gap-2 rounded-full border border-border bg-card px-2 py-1 text-sm text-foreground">
        {user.avatarUrl ? (
          <Image
            src={user.avatarUrl}
            alt=""
            width={24}
            height={24}
            className="size-6 rounded-full"
          />
        ) : (
          <span className="flex size-6 items-center justify-center rounded-full bg-muted text-xs font-medium text-muted-foreground">
            {initials(user.displayName)}
          </span>
        )}
        <span className="max-w-32 truncate">{user.displayName}</span>
      </DropdownMenuTrigger>
      {/* `w-auto` overrides the primitive's default `w-(--radix-dropdown-menu-trigger-width)`
          (components/ui/dropdown-menu.tsx). Sizing a menu to its trigger is a fine default when the
          trigger is the widest thing in play; here it is not — the trigger is a display name capped
          at `max-w-32`, and the menu holds a full email address, which is reliably longer. Pinned
          to the trigger, the content's `overflow-x-hidden` chopped the address mid-character.
          `min-w-*` keeps the menu from being narrower than the pill that opened it, and `max-w-*`
          is Radix's own measurement of the room left before the viewport edge, so a long address
          cannot push the menu off a 375px screen. */}
      <DropdownMenuContent
        align="end"
        className="w-auto max-w-(--radix-dropdown-menu-content-available-width) min-w-(--radix-dropdown-menu-trigger-width)"
      >
        {user.email ? (
          // `truncate` is the last resort for an address too long even for the space Radix
          // measured: an ellipsis reads as "shortened", a hard cut reads as a rendering bug.
          //
          // The extra padding is on BOTH rows, not just this one. Widening only the label would
          // indent the address relative to `Sign out`, and moving it to the menu's own `p-1`
          // would un-flush the separator, whose `-mx-1` is written against that exact value.
          <DropdownMenuLabel className="truncate px-2 py-1.5 font-normal text-muted-foreground">
            {user.email}
          </DropdownMenuLabel>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem className="px-2" onSelect={handleSignOut}>
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
