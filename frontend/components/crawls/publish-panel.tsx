"use client";

import { useEffect, useId, useState } from "react";
import { CheckIcon, ExternalLinkIcon, GitPullRequestIcon, Lock, XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Installation, Publication, PublishMode } from "@/lib/api/publish";
import { ownerIdentity } from "@/lib/crawls/owner";
import { publishStatusCopy } from "@/lib/crawls/publish-copy";
import { useDeletePublishTarget } from "@/lib/query/use-delete-publish-target";
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
 * Lives in the Schedule tab beside `EnrichmentPanel`, for the reason that panel's docstring gives
 * at length: the Schedule tab is already the only place an owner changes anything about a website,
 * and a fifth tab would mean editing `DETAIL_TABS`, the URL vocabulary and `useDetailView`'s guard
 * for one form. It is also the right *conceptual* home — publishing is what a schedule is FOR,
 * and reading "run daily" directly above "open a pull request when it changes" is the whole
 * feature in two sentences.
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
 * `<a>` to `github.com/apps/{slug}/installations/new`, and the return trip lands on
 * `app/api/github/callback/route.ts`. `NEXT_PUBLIC_GITHUB_APP_SLUG` is the one piece of this
 * feature the browser needs, and it is safe to expose: an App slug is public by construction —
 * it is in the URL of the App's own listing page.
 */
export function PublishPanel({
  websiteId,
  ownerUserId,
  isOwner,
}: {
  websiteId: string;
  /** `null` while the website query is still resolving. */
  ownerUserId: string | null;
  isOwner: boolean;
}) {
  const installations = useInstallations();
  const target = usePublishTarget(websiteId);

  if (installations.isPending || target.isPending) return <PublishPanelSkeleton />;

  // Same precedence rule `EnrichmentPanel` and `ScheduleTab` state: not being the owner is the
  // only reason this panel is ever inert, so there is one branch rather than a precedence list.
  const disabledReason = isOwner
    ? null
    : ownerUserId === null
      ? "Only the owner can change this."
      : `Only the owner (${ownerIdentity(ownerUserId, null).label}) can change this.`;

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
        <ConnectPrompt disabledReason={disabledReason} />
      ) : (
        <TargetForm
          websiteId={websiteId}
          installations={connected}
          existing={target.data ?? null}
          disabledReason={disabledReason}
        />
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
function ConnectPrompt({ disabledReason }: { disabledReason: string | null }) {
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

  return (
    <div className="space-y-2">
      <Button asChild variant="outline" size="sm">
        {/* A plain anchor, not `next/link`: this leaves the app entirely, and GitHub returns the
            user through a fresh top-level navigation to our callback route. */}
        <a href={`https://github.com/apps/${slug}/installations/new`}>
          Connect a GitHub repository
          <ExternalLinkIcon aria-hidden />
        </a>
      </Button>
      <p className="text-xs text-muted-foreground">
        You choose which repositories to grant. You can revoke it any time from your GitHub
        settings.
      </p>
    </div>
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
          <select
            id={`${pathId}-account`}
            value={installationId}
            disabled={disabled}
            onChange={(event) => {
              setInstallationId(event.target.value);
              // Clear the repository: it belonged to the previous account's installation and
              // almost certainly is not reachable from the new one.
              setRepo("");
              setBranch("");
            }}
            className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm"
          >
            {installations.map((installation) => (
              <option key={installation.id} value={installation.id}>
                {installation.account_login}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="space-y-1.5">
        <Label htmlFor={`${pathId}-repo`} className="text-xs">
          Repository
        </Label>
        {/* A `datalist` rather than a `<select>`: the repository list can be truncated at 100
            (the API says so with `truncated`), and an installation with more than that needs a
            field the user can type into. One control that both suggests and accepts free text is
            strictly better than a picker that silently omits the repository they wanted. */}
        <Input
          id={`${pathId}-repo`}
          list={`${pathId}-repos`}
          value={repo}
          disabled={disabled}
          placeholder="owner/repository"
          onChange={(event) => {
            setRepo(event.target.value);
            const match = (repositories.data?.repositories ?? []).find(
              (candidate) => `${candidate.owner}/${candidate.name}` === event.target.value
            );
            // Prefill the branch from the repository's own default the moment one is recognized.
            // Storing it rather than resolving it per run is deliberate on the backend (a
            // default-branch rename must not silently retarget a publication), so this is the one
            // place the default is read at all.
            if (match !== undefined) setBranch(match.default_branch);
          }}
        />
        <datalist id={`${pathId}-repos`}>
          {(repositories.data?.repositories ?? []).map((candidate) => (
            <option key={`${candidate.owner}/${candidate.name}`} value={`${candidate.owner}/${candidate.name}`} />
          ))}
        </datalist>
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
