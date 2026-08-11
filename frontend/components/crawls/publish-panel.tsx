"use client";

import { useEffect, useId, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ExternalLinkIcon,
  GitPullRequestIcon,
  Lock,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Installation, Publication, PublishMode } from "@/lib/api/publish";
import { publishStatusCopy, PUBLISH_NEXT_STEPS } from "@/lib/crawls/publish-copy";
import { useDeletePublishTarget } from "@/lib/query/use-delete-publish-target";
import { useDisconnectInstallation } from "@/lib/query/use-disconnect-installation";
import { useInstallations } from "@/lib/query/use-installations";
import { usePublications } from "@/lib/query/use-publications";
import { usePublishTarget } from "@/lib/query/use-publish-target";
import { usePutPublishTarget } from "@/lib/query/use-put-publish-target";
import { useRepositories } from "@/lib/query/use-repositories";
import { cn } from "@/lib/utils";

import { RelativeTime } from "./relative-time";

/**
 * Where this site's `llms.txt` gets published, and what happened last time.
 *
 * Has its own tab — `publish`, in `DETAIL_TABS` — unlike `EnrichmentPanel`, which stayed a card
 * inside the Schedule tab (see that component's own docstring for the argument it makes for
 * itself). The two look similar at a glance — both are per-website configuration a card renders
 * beside the schedule controls — but they differ on the one axis that actually decides whether
 * something earns a tab: `EnrichmentPanel` is one checkbox with an immediate, local effect;
 * publishing is a multi-step configuration with an EXTERNAL round trip that has to be returned
 * to. Connecting a GitHub account leaves this application entirely, and a URL that names the
 * destination (`?tab=publish`) is what makes that return trip expressible at all —
 * `app/api/github/callback/route.ts` can redirect to a specific website's Publish tab only
 * because the tab exists as an addressable `?tab=` value in the first place. A checkbox with no
 * round trip has nothing to return to, which is exactly why it did not move.
 *
 * ## Three states, and the first one is the interesting one
 *
 * 1. **No GitHub account connected.** The panel is a single call to action pointing at GitHub's
 *    own installation page. It does not pretend to be a form that cannot be submitted.
 * 2. **Connected, no target.** The form, unsaved.
 * 3. **Connected with a target.** The form, populated, plus the publication history.
 *
 * State 1 is deliberately NOT rendered as a disabled version of state 2. A repository picker with
 * nothing to pick from and a Save button that cannot work reads as a broken page; a link that says
 * what to do next does not.
 *
 * ## The install link leaves the app, and that is unavoidable
 *
 * A GitHub App installation happens on github.com — the user chooses which repositories to grant,
 * which is precisely the decision this product must not make for them. So the button is an
 * `<a>` to `github.com/apps/{slug}/installations/new?state={websiteId}`, and the return trip
 * lands on `app/api/github/callback/route.ts`. `NEXT_PUBLIC_GITHUB_APP_SLUG` is the one piece of
 * this feature the browser needs, and it is safe to expose: an App slug is public by construction
 * — it is in the URL of the App's own listing page.
 *
 * `state` is what closes the round trip this docstring's first paragraph argues for: GitHub
 * echoes it back verbatim on the redirect, and the callback route hands it to
 * `lib/crawls/github-callback.ts`'s `githubCallbackPath`, which sends the user back to THIS
 * website's Publish tab rather than the flat `/crawls` list — but only after validating it as a
 * UUID first, because `state` arrives on a browser redirect this application does not control
 * the query string of, exactly as attacker-suppliable as `installation_id` already is (see that
 * route's own header comment). `websiteId` is safe to put there in the first place for the same
 * reason `installation_id` is safe to trust nothing from: nobody can do anything with a website
 * id they could not already do by typing `/crawls/{id}` into the address bar, since reads are
 * unscoped (ARCHITECTURE.md §4).
 */
export function PublishPanel({
  websiteId,
  ownerLabel,
  isOwner,
}: {
  websiteId: string;
  /** The owner's resolved identity — `@handle`, a display name, or a short id fallback,
   * never "you" (`website-detail.tsx`'s `ownerLabel`, via `lib/crawls/owner.ts`'s
   * `ownerIdentity`) — for the "Only the owner (…) can …" copy below. `null` while the
   * website query is still resolving. */
  ownerLabel: string | null;
  isOwner: boolean;
}) {
  const installations = useInstallations();
  const target = usePublishTarget(websiteId);

  if (installations.isPending || target.isPending) return <PublishPanelSkeleton />;

  // Same precedence rule `EnrichmentPanel` and `ScheduleTab` state: not being the owner is the
  // only reason this panel is ever inert, so there is one branch rather than a precedence list.
  const disabledReason = isOwner
    ? null
    : ownerLabel === null
      ? "Only the owner can change this."
      : `Only the owner (${ownerLabel}) can change this.`;

  const connected = installations.data ?? [];

  return (
    <div className="mx-auto w-full max-w-xl space-y-4 rounded-lg border border-border bg-card p-6">
      <div className="space-y-1">
        <h3 className="text-sm font-medium text-foreground">Publish to GitHub</h3>
        <p className="text-xs text-muted-foreground">
          When a run finds the generated <code className="font-mono">llms.txt</code> has changed,
          open a pull request with the new file. Nothing is committed when nothing changed.
        </p>
      </div>

      {connected.length === 0 ? (
        <ConnectPrompt websiteId={websiteId} disabledReason={disabledReason} />
      ) : (
        <>
          {/* State 2 only: connected, but nothing saved yet. Once a target exists the form
              below already says what it does; this line exists for the visit — often the very
              one that just came back from GitHub — where the form is empty and a person has to
              be told there is a second step at all. Gated on `disabledReason` too: telling a
              non-owner about two steps they cannot take is noise, not help. */}
          {(target.data ?? null) === null && disabledReason === null && (
            <p className="text-xs text-muted-foreground">{PUBLISH_NEXT_STEPS}</p>
          )}
          <TargetForm
            websiteId={websiteId}
            installations={connected}
            existing={target.data ?? null}
            disabledReason={disabledReason}
          />
          <ConnectedAccounts installations={connected} disabled={disabledReason !== null} />
        </>
      )}

      {target.data !== null && target.data !== undefined && (
        <PublicationHistory websiteId={websiteId} />
      )}

      {disabledReason !== null && (
        <p className="flex items-start gap-2 border-t border-border pt-3 text-sm text-muted-foreground">
          <Lock aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <span>{disabledReason}</span>
        </p>
      )}
    </div>
  );
}

/** Matches the loaded card's shape, mirroring `EnrichmentPanel`'s own skeleton. */
function PublishPanelSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="mx-auto w-full max-w-xl space-y-3 rounded-lg border border-border bg-card p-6"
    >
      <Skeleton className="h-4 w-36" />
      <Skeleton className="h-3 w-full max-w-sm" />
      <Skeleton className="h-9 w-48" />
    </div>
  );
}

/**
 * State 1: no installation. A link out to GitHub, or an explanation of why there is no link.
 *
 * The slug is read at render rather than at module scope so a deployment that has not set it
 * renders the "not configured" copy instead of a link to `github.com/apps/undefined`.
 */
function ConnectPrompt({
  websiteId,
  disabledReason,
}: {
  websiteId: string;
  disabledReason: string | null;
}) {
  const slug = process.env.NEXT_PUBLIC_GITHUB_APP_SLUG;

  if (slug === undefined || slug === "") {
    return (
      <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
        Publishing to GitHub isn&apos;t configured for this deployment.
      </p>
    );
  }

  if (disabledReason !== null) {
    return (
      <p className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
        This site&apos;s owner hasn&apos;t connected a GitHub account.
      </p>
    );
  }

  // `state` is this website's own id, so GitHub's redirect back can name which site to return
  // to — see this module's own docstring for the round trip it closes and the validation
  // `githubCallbackPath` applies before trusting it. Built with `URLSearchParams`, not string
  // concatenation, and the slug is escaped too: an App slug is operator-configured, not
  // hardcoded, so nothing about this URL should assume it needs no escaping just because this
  // deployment's own slug currently happens not to need any.
  const installParams = new URLSearchParams({ state: websiteId });
  const installUrl = `https://github.com/apps/${encodeURIComponent(slug)}/installations/new?${installParams.toString()}`;

  return (
    <div className="space-y-2">
      <Button asChild variant="outline" size="sm">
        {/* A plain anchor, not `next/link`: this leaves the app entirely, and GitHub returns the
            user through a fresh top-level navigation to our callback route. */}
        <a href={installUrl}>
          Connect a GitHub repository
          <ExternalLinkIcon aria-hidden />
        </a>
      </Button>
      <p className="text-xs text-muted-foreground">
        You choose which repositories to grant. You can revoke it any time from your GitHub
        settings. GitHub will bring you back here when you&apos;re done — you&apos;ll still need
        to pick a repository and turn on publishing before anything is written.
      </p>
    </div>
  );
}

/**
 * The picker both fields below use — the same control the Output tab's run picker is: a
 * `DropdownMenu` with a `DropdownMenuRadioGroup` inside it, opened by an outline `Button`.
 *
 * A native `<select>` would be fewer lines and is what these two fields were first. It is not what
 * the rest of this application means by a dropdown, though, and that difference is not cosmetic
 * here: this field names the repository a robot will open pull requests against, so it has to look
 * like a control whose options are a fixed, known list rather than a box that happens to have an
 * arrow. `output-tab.tsx`'s `RunPicker` established the shape; this reuses it rather than
 * introducing a third kind of dropdown.
 *
 * `type="button"` is load-bearing: this trigger lives inside a `<form>`, where a button with no
 * type submits it.
 */
function PickerField({
  id,
  value,
  placeholder,
  options,
  disabled,
  onSelect,
}: {
  id: string;
  /** The selected option's `value`, or `""` for none — in which case the trigger shows
   * `placeholder`. What the trigger renders is that option's `label`, never `value` itself: the
   * account picker's value is an installation uuid, which is exactly the thing a person must not
   * be shown. */
  value: string;
  placeholder: string;
  options: { value: string; label: string }[];
  disabled: boolean;
  onSelect: (value: string) => void;
}) {
  const selected = options.find((option) => option.value === value);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          disabled={disabled}
          className="w-full justify-between gap-3 font-normal"
        >
          <span
            className={cn("min-w-0 truncate", selected === undefined && "text-muted-foreground")}
          >
            {selected?.label ?? placeholder}
          </span>
          <ChevronDownIcon aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      {/* Capped and scrollable, and allowed to outgrow its trigger — an installation may grant a
          hundred repositories, and `owner/repository` is longer than the field is wide more often
          than not. Same sizing `RunPicker` arrived at, including the `2.75rem` each row spends on
          the radio checkmark that the trigger does not. */}
      <DropdownMenuContent className="max-h-80 w-auto max-w-(--radix-dropdown-menu-content-available-width) min-w-[calc(var(--radix-dropdown-menu-trigger-width)+2.75rem)] overflow-y-auto">
        <DropdownMenuRadioGroup value={value} onValueChange={onSelect}>
          {options.map((option) => (
            <DropdownMenuRadioItem key={option.value} value={option.value} className="py-1.5">
              <span className="whitespace-nowrap">{option.label}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * States 2 and 3: the form.
 *
 * Local state seeded from `existing` and re-seeded when it changes — `useEffect` on the target's
 * `id` and not on the whole object, because the query refetches after every save and a new object
 * identity with identical fields would otherwise reset a form the user had already started
 * editing again.
 */
function TargetForm({
  websiteId,
  installations,
  existing,
  disabledReason,
}: {
  websiteId: string;
  installations: Installation[];
  existing: import("@/lib/api/publish").PublishTarget | null;
  disabledReason: string | null;
}) {
  const pathId = useId();
  const put = usePutPublishTarget();
  const remove = useDeletePublishTarget();

  const [installationId, setInstallationId] = useState(
    existing?.installation_id ?? installations[0]?.id ?? ""
  );
  const [repo, setRepo] = useState(
    existing !== null ? `${existing.repo_owner}/${existing.repo_name}` : ""
  );
  const [branch, setBranch] = useState(existing?.base_branch ?? "");
  const [path, setPath] = useState(existing?.path ?? "llms.txt");
  const [mode, setMode] = useState<PublishMode>(existing?.mode ?? "pull_request");
  const [active, setActive] = useState(existing?.active ?? false);

  // Re-seed when the target this panel is editing genuinely changes identity.
  useEffect(() => {
    if (existing === null) return;
    setInstallationId(existing.installation_id);
    setRepo(`${existing.repo_owner}/${existing.repo_name}`);
    setBranch(existing.base_branch);
    setPath(existing.path);
    setMode(existing.mode);
    setActive(existing.active);
  }, [existing?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const repositories = useRepositories(installationId === "" ? null : installationId);
  const disabled = disabledReason !== null;

  const repoNames = (repositories.data?.repositories ?? []).map(
    (candidate) => `${candidate.owner}/${candidate.name}`
  );
  // The saved repository stays selectable even when it is not in the list GitHub just returned —
  // an installation whose access was edited would otherwise render a blank `<select>` over a value
  // the form still holds and would still save.
  const repoOptions = repo !== "" && !repoNames.includes(repo) ? [...repoNames, repo] : repoNames;
  // Whether the list we have IS the whole list. `truncated` says it is not, and an error means we
  // have no list at all; either way the field has to accept a name we cannot offer.
  const canPickRepo = !repositories.isError && repositories.data?.truncated !== true;

  // Choosing a repository prefills the branch from that repository's own default the moment one is
  // recognized. Storing it rather than resolving it per run is deliberate on the backend (a
  // default-branch rename must not silently retarget a publication), so this is the one place the
  // default is read at all.
  const chooseRepo = (value: string) => {
    setRepo(value);
    const match = (repositories.data?.repositories ?? []).find(
      (candidate) => `${candidate.owner}/${candidate.name}` === value
    );
    if (match !== undefined) setBranch(match.default_branch);
  };

  // `owner/name`, split at the FIRST slash only. A repository name cannot contain one, so
  // everything after the first is invalid input rather than part of the name — and the backend's
  // `_REPO_SEGMENT` will reject it, which is where that decision belongs.
  const [repoOwner, ...repoRest] = repo.split("/");
  const repoName = repoRest.join("/");
  const canSubmit =
    !disabled &&
    installationId !== "" &&
    repoOwner !== "" &&
    repoName !== "" &&
    branch.trim() !== "" &&
    path.trim() !== "";

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!canSubmit) return;
        put.mutate({
          websiteId,
          body: {
            installation_id: installationId,
            repo_owner: repoOwner,
            repo_name: repoName,
            base_branch: branch.trim(),
            path: path.trim(),
            mode,
            active,
          },
        });
      }}
    >
      {installations.length > 1 && (
        <div className="space-y-1.5">
          <Label htmlFor={`${pathId}-account`} className="text-xs">
            GitHub account
          </Label>
          <PickerField
            id={`${pathId}-account`}
            value={installationId}
            placeholder="Select an account"
            options={installations.map((installation) => ({
              value: installation.id,
              label: installation.account_login,
            }))}
            disabled={disabled}
            onSelect={(id) => {
              setInstallationId(id);
              // Clear the repository: it belonged to the previous account's installation and
              // almost certainly is not reachable from the new one.
              setRepo("");
              setBranch("");
            }}
          />
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor={`${pathId}-repo`} className="text-xs">
          Repository
        </Label>
        {/* A picker when we can list every repository the installation grants, and a free-text
            field when we cannot. The set of repositories is a decision the user already made on
            GitHub, and it is the entire set of destinations that can work at all — a field you can
            type into implies some other repository might be reachable if only you spelled it
            right, and typing one buys a `404` at publish time.

            The exception is an installation granting more repositories than one page carries (the
            API says so with `truncated`) or a repositories call that failed: there the list
            genuinely is not the whole list, and a picker that silently omits what someone wanted
            is worse than a text box. */}
        {canPickRepo ? (
          <PickerField
            id={`${pathId}-repo`}
            value={repo}
            placeholder={repositories.isPending ? "Loading repositories…" : "Select a repository"}
            options={repoOptions.map((candidate) => ({ value: candidate, label: candidate }))}
            disabled={disabled || repositories.isPending || repoOptions.length === 0}
            onSelect={chooseRepo}
          />
        ) : (
          <>
            <Input
              id={`${pathId}-repo`}
              list={`${pathId}-repos`}
              value={repo}
              disabled={disabled}
              placeholder="owner/repository"
              onChange={(event) => chooseRepo(event.target.value)}
            />
            <datalist id={`${pathId}-repos`}>
              {repoNames.map((candidate) => (
                <option key={candidate} value={candidate} />
              ))}
            </datalist>
          </>
        )}
        {canPickRepo && !repositories.isPending && repoOptions.length === 0 && (
          <p className="text-xs text-muted-foreground">
            This installation doesn&apos;t grant access to any repository yet. Add one in your
            GitHub settings, then reopen this tab.
          </p>
        )}
        {repositories.data?.truncated === true && (
          <p className="text-xs text-muted-foreground">
            This installation has more repositories than we can list — type the full{" "}
            <code className="font-mono">owner/name</code>.
          </p>
        )}
        {repositories.isError && (
          <p className="text-xs text-muted-foreground">
            Couldn&apos;t list repositories from GitHub. You can still type one.
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1.5">
          <Label htmlFor={`${pathId}-branch`} className="text-xs">
            Branch
          </Label>
          <Input
            id={`${pathId}-branch`}
            value={branch}
            disabled={disabled}
            placeholder="main"
            onChange={(event) => setBranch(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`${pathId}-path`} className="text-xs">
            Path in the repo
          </Label>
          <Input
            id={`${pathId}-path`}
            value={path}
            disabled={disabled}
            placeholder="llms.txt"
            onChange={(event) => setPath(event.target.value)}
          />
        </div>
      </div>

      <div className="space-y-2">
        <Label className="text-xs">How to publish</Label>
        <RadioGroup
          value={mode}
          disabled={disabled}
          onValueChange={(value) => setMode(value as PublishMode)}
          className="gap-2"
        >
          <label className="flex items-start gap-2 text-xs text-muted-foreground">
            <RadioGroupItem value="pull_request" className="mt-0.5" />
            <span>
              <span className="font-medium text-foreground">Open a pull request</span> — reviewable
              before it lands. Recommended.
            </span>
          </label>
          <label className="flex items-start gap-2 text-xs text-muted-foreground">
            <RadioGroupItem value="commit" className="mt-0.5" />
            <span>
              <span className="font-medium text-foreground">Commit directly</span> — writes straight
              to the branch above.
            </span>
          </label>
        </RadioGroup>
      </div>

      <div className="flex items-start gap-3 border-t border-border pt-3">
        <Switch
          checked={active}
          disabled={disabled}
          onCheckedChange={setActive}
          aria-label="Publish on every successful run"
        />
        <div className="space-y-0.5">
          <p className="text-sm font-medium text-foreground">Publish on every successful run</p>
          <p className="text-xs text-muted-foreground">
            Off until you turn it on — connecting a repository and writing to it are two different
            decisions.
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2">
        {disabled ? (
          <Tooltip>
            {/* A disabled button dispatches no pointer events, so the tooltip needs a focusable
                wrapper — the same workaround `ScheduleControls` and `EnrichmentPanel` use. */}
            <TooltipTrigger asChild>
              <span tabIndex={0} className="inline-flex rounded-sm">
                <Button type="submit" size="sm" disabled>
                  Save
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>{disabledReason}</TooltipContent>
          </Tooltip>
        ) : (
          <Button type="submit" size="sm" disabled={!canSubmit || put.isPending}>
            {put.isPending ? "Saving…" : "Save"}
          </Button>
        )}
        {existing !== null && !disabled && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => remove.mutate(websiteId)}
          >
            Stop publishing
          </Button>
        )}
      </div>
    </form>
  );
}

/**
 * The connected accounts, with a way to disconnect each.
 *
 * A user must be able to undo the connection from the app they made it in — telling them to go to
 * GitHub for it would be the kind of dead end this panel exists to avoid. It does not *uninstall*
 * the App, though, and the copy says so: uninstalling is the user's own action in their GitHub
 * settings, and an API that could revoke its own access on someone's behalf is a worse tool than
 * one that is honest about the boundary. Disconnecting here forgets the installation and deletes
 * every publish target that used it.
 *
 * Rendered below the form rather than above it: on the common path a user is here to configure a
 * repository, and account management is the rarer errand.
 */
function ConnectedAccounts({
  installations,
  disabled,
}: {
  installations: Installation[];
  disabled: boolean;
}) {
  const disconnect = useDisconnectInstallation();

  return (
    <div className="space-y-1.5 border-t border-border pt-3">
      <p className="text-xs font-medium text-foreground">Connected GitHub accounts</p>
      <ul className="space-y-1">
        {installations.map((installation) => (
          <li key={installation.id} className="flex items-center justify-between gap-3 text-xs">
            <span className="min-w-0 truncate text-muted-foreground">
              {installation.account_login}
            </span>
            {!disabled && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-6 px-2 text-xs"
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate(installation.id)}
              >
                Disconnect
              </Button>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted-foreground">
        Disconnecting stops publishing and forgets the connection. To remove the app&apos;s access
        entirely, uninstall it in your GitHub settings.
      </p>
    </div>
  );
}

/**
 * State 3's history. Polled, because a publication is written by the WORKER and nothing in the
 * browser causes it — see `usePublications` for why this is the one query in the feature that
 * polls at all.
 */
function PublicationHistory({ websiteId }: { websiteId: string }) {
  const publications = usePublications(websiteId);
  const rows = publications.data ?? [];

  if (publications.isPending) return <Skeleton className="h-16 w-full rounded-md" />;

  if (rows.length === 0) {
    return (
      <p className="border-t border-border pt-3 text-xs text-muted-foreground">
        Nothing published yet. The next run that changes the index will open a pull request.
      </p>
    );
  }

  return (
    <div className="space-y-2 border-t border-border pt-3">
      <p className="text-xs font-medium text-foreground">Recent publications</p>
      <ul className="space-y-1.5">
        {rows.slice(0, 5).map((publication) => (
          <PublicationRow key={publication.id} publication={publication} />
        ))}
      </ul>
    </div>
  );
}

function PublicationRow({ publication }: { publication: Publication }) {
  const copy = publishStatusCopy(publication.status);
  const Icon =
    publication.status === "succeeded"
      ? GitPullRequestIcon
      : publication.status === "failed"
        ? XIcon
        : CheckIcon;

  return (
    <li className="flex items-start gap-2 text-xs">
      <Icon aria-hidden className={cn("mt-0.5 size-3.5 shrink-0", copy.tone)} />
      <span className="min-w-0 flex-1">
        <span className={copy.tone}>{copy.label}</span>
        <span className="text-muted-foreground">
          {" · "}
          <RelativeTime iso={publication.created_at} className="text-xs" />
        </span>
        {publication.pr_url !== null && (
          <>
            {" · "}
            <a
              href={publication.pr_url}
              target="_blank"
              rel="noreferrer noopener"
              className="text-primary underline-offset-4 hover:underline"
            >
              #{publication.pr_number}
            </a>
          </>
        )}
        {/* The failure reason, shown rather than hidden behind a tooltip: it is the only thing on
            this row a user can act on, and it comes from GitHub ("protected branch", "not
            accessible by integration") rather than from us. */}
        {publication.error !== null && (
          <span className="mt-0.5 block text-muted-foreground">{publication.error}</span>
        )}
      </span>
    </li>
  );
}
