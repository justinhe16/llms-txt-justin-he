"""Connecting a GitHub repository, and publishing a run's `llms.txt` to it.

Two audiences, one service. The API-facing half configures a target — `require_owner` on the
parent website, exactly as `ScheduleService.upsert_schedule` does. The worker-facing half
(`publish_run`) is called from the crawl pipeline after a run succeeds and has no request, no JWT
and no owner to check, the same footing `RunService.record_success` already stands on.

## `publish_run` CANNOT fail a run, and that is enforced here rather than asked of callers

This is the load-bearing rule of the module. By the time it runs, the artifact is already
generated, already uploaded, and already committed to `runs.llms_txt` — the user's `llms.txt`
exists and is correct. A failure to copy it into a git repository is a failure of a *delivery*
step, and letting it mark the run failed would replace a good artifact with none at all.

So `publish_run` catches `Exception` around the whole GitHub interaction and records a `failed`
publication row instead of propagating. The one thing it deliberately does not catch is
`asyncio.CancelledError` — see the `except` clause for why swallowing that is a different and
worse bug.

## Order of operations, and where the transaction is

Every network call happens with no transaction open (ARCHITECTURE.md §5.1), and the database is
touched at the two ends:

1. Read the target and any settled publication for this run. (short read, no transaction)
2. Talk to GitHub: token, read the current file, maybe branch, write, maybe open a PR. (network)
3. Open one short transaction and record the outcome. (write)

Step 2 is the slow part and is exactly what must not sit inside a transaction — a protected-branch
rejection can take a second, and a Postgres transaction held across it for every publishing
website is how a connection pool is exhausted.

## Idempotency, because a worker job can be retried

A run whose job is retried must not open a second pull request for the same artifact.
`find_settled_publication` is asked first, and any non-`failed` row for this `(run_id, target_id)`
short-circuits the whole method. `failed` rows deliberately do not count: those are the ones worth
retrying.
"""

import logging
from typing import Any, Final
from uuid import UUID

import httpx
from asyncpg import Pool
from fastapi import HTTPException, status

from app.core.auth.ownership import require_owner
from app.core.settings import settings
from app.features.publish.internals.change_summary import change_summary, index_changed
from app.features.publish.internals.github_app import (
    GitHubAuthError,
    fetch_installation_account,
    forget_installation_token,
    installation_token,
)
from app.features.publish.internals.github_client import (
    GitHubApiError,
    branch_head_sha,
    create_branch,
    create_pull_request,
    list_repositories,
    read_file,
    write_file,
)
from app.features.publish.internals.publish_reader import PublishReader
from app.features.publish.internals.publish_writer import PublishWriter
from app.features.publish.schemas import (
    InstallationResponse,
    PublicationResponse,
    PublishTargetRequest,
    PublishTargetResponse,
    RepositoryListResponse,
    RepositoryResponse,
)
from app.features.websites.service import WebsiteService
from app.infrastructure.db.transaction import transaction


logger = logging.getLogger(__name__)

MAX_PUBLICATIONS_PAGE: Final = 50
"""How many publications `list_publications` returns. A fixed ceiling rather than a paginated
endpoint: this history is a panel on a website's page, not a data export, and `Page[...]`
(`app.core.pagination`) would be machinery for a list that is almost always length zero or one."""

_ERROR_COLUMN_LIMIT: Final = 500
"""How much of a failure message reaches `publications.error`.

Bounded because the text can originate from GitHub, which is to say from outside this codebase. An
unbounded error string in a column every history query selects is the same mistake a `SELECT *`
would be, arrived at from a different direction.
"""


class PublishService:
    """Publish targets, installations, and the worker's publish step."""

    def __init__(self, pool: Pool, websites: WebsiteService) -> None:
        self._pool = pool
        self._websites = websites

    # -----------------------------------------------------------------------------------
    # Installations
    # -----------------------------------------------------------------------------------

    async def list_installations(self, user_id: UUID) -> list[InstallationResponse]:
        """Every GitHub App installation this user has connected."""
        rows = await PublishReader(self._pool).list_installations(user_id)
        return [InstallationResponse(**row) for row in rows]

    async def connect_installation(
        self, installation_id: int, user_id: UUID, client: httpx.AsyncClient
    ) -> InstallationResponse:
        """Record an installation GitHub redirected this user back from.

        `installation_id` arrives as a query parameter on a browser redirect, which makes it an
        untrusted number. It is verified against GitHub with this deployment's own App credential
        BEFORE anything is written — `fetch_installation_account` is that check, and its `404` is
        what an id somebody typed by hand looks like from here.

        The verification is also the network call, so it happens before the transaction opens.
        """
        self._require_publishing_enabled()
        try:
            account_login = await fetch_installation_account(client, installation_id)
        except GitHubAuthError as exc:
            # A 404 from GitHub and a missing private key both land here, and they are different
            # problems — one is the caller's, one is the deployment's. Both are reported as a
            # `400` rather than a `500` because neither is recoverable by retrying, and the
            # message (which never contains a credential — see `GitHubAuthError`) says which.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not verify that GitHub installation: {exc}",
            ) from None

        async with transaction(self._pool) as tx:
            row = await PublishWriter(tx).upsert_installation(
                user_id, installation_id, account_login
            )
        logger.info(
            "publish: installation connected",
            # `installation_id` and the account name are not secrets; no token is minted here and
            # none is logged anywhere in this module.
            extra={"installation_id": installation_id, "account_login": account_login},
        )
        return InstallationResponse(**row)

    async def disconnect_installation(self, installation_row_id: UUID, user_id: UUID) -> None:
        """Forget an installation, and every publish target that pointed at it.

        The targets go first, in the same transaction: the foreign key is `ON DELETE RESTRICT`, so
        deleting the installation alone would raise. Doing it in this order, explicitly, is the
        design `schema.prisma` describes — the alternative, `ON DELETE CASCADE`, would silently
        delete a user's publishing configuration as a side effect of disconnecting an account.

        This does NOT uninstall the App on GitHub. It cannot: uninstalling is the user's action to
        take in their own account, and an API that could revoke its own access on the user's
        behalf would be a worse tool than one that says so.
        """
        reader = PublishReader(self._pool)
        installation = await reader.get_installation_for_user(installation_row_id, user_id)
        if installation is None:
            # `404` for both "no such row" and "not yours" — see `get_installation_for_user` on
            # why this one case does not get the `403` treatment §4.2 gives websites.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such GitHub installation"
            )

        async with transaction(self._pool) as tx:
            writer = PublishWriter(tx)
            # Order matters and is enforced by the schema: `publish_targets.installation_row_id`
            # is `ON DELETE RESTRICT`, so the installation delete raises unless its targets are
            # gone first. Both in one transaction, so a failure leaves neither half applied.
            await writer.delete_targets_for_installation(installation_row_id)
            await writer.delete_installation(installation_row_id, user_id)

        # Drop the cached token too. Harmless if absent, and it keeps a disconnected
        # installation's credential from lingering in this process's memory for up to
        # `github_token_ttl_s`.
        forget_installation_token(int(installation["installation_id"]))

    async def list_installation_repositories(
        self, installation_row_id: UUID, user_id: UUID, client: httpx.AsyncClient
    ) -> RepositoryListResponse:
        """The repositories this installation may write to, for the target picker."""
        self._require_publishing_enabled()
        reader = PublishReader(self._pool)
        installation = await reader.get_installation_for_user(installation_row_id, user_id)
        if installation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No such GitHub installation"
            )

        github_id = int(installation["installation_id"])
        try:
            token = await installation_token(client, github_id)
            repositories, truncated = await list_repositories(client, token.token)
        except (GitHubAuthError, GitHubApiError) as exc:
            forget_installation_token(github_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Could not list repositories from GitHub: {exc}",
            ) from None

        return RepositoryListResponse(
            repositories=[
                RepositoryResponse(
                    owner=repository.owner,
                    name=repository.name,
                    default_branch=repository.default_branch,
                    private=repository.private,
                )
                for repository in repositories
            ],
            truncated=truncated,
        )

    # -----------------------------------------------------------------------------------
    # Targets
    # -----------------------------------------------------------------------------------

    async def get_target(self, website_id: UUID) -> PublishTargetResponse | None:
        """A website's publish target, or `None`.

        Unscoped, like every other read in this codebase (§4.1) — `GET` needs a valid JWT and
        nothing more. A target names a repository the site's owner chose, which is the same
        category of fact as the schedule sitting beside it.
        """
        row = await PublishReader(self._pool).get_target_by_website(website_id)
        return None if row is None else _to_target(row)

    async def upsert_target(
        self, website_id: UUID, request: PublishTargetRequest, user_id: UUID
    ) -> PublishTargetResponse:
        """Create or replace where this website publishes.

        Fetch the website, `require_owner` on it with nothing in between, then validate the
        installation and write — the exact order §4.2 requires and `ScheduleService.upsert_
        schedule` already follows.
        """
        website = await self._websites.get_website(website_id)
        require_owner(website, user_id)  # 403 — nothing between these two lines

        # The installation must be one THIS user connected. Without this check a user could point
        # their own website at somebody else's installation row and publish through a credential
        # they were never granted — the one genuine privilege-escalation path in this feature.
        reader = PublishReader(self._pool)
        installation = await reader.get_installation_for_user(request.installation_id, user_id)
        if installation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No such GitHub installation for this user",
            )

        async with transaction(self._pool) as tx:
            await PublishWriter(tx).upsert_target(
                website_id,
                installation_row_id=request.installation_id,
                repo_owner=request.repo_owner,
                repo_name=request.repo_name,
                base_branch=request.base_branch,
                path=request.path,
                mode=request.mode,
                active=request.active,
            )

        # Re-read through the reader rather than mapping the `RETURNING` row, because the response
        # carries `account_login` from the joined installation and the writer's row does not. One
        # extra small read buys a response shape that cannot disagree with `GET`'s.
        row = await reader.get_target_by_website(website_id)
        assert row is not None, "target was just written"
        return _to_target(row)

    async def delete_target(self, website_id: UUID, user_id: UUID) -> None:
        """Stop publishing this website. Idempotent — deleting nothing is not an error."""
        website = await self._websites.get_website(website_id)
        require_owner(website, user_id)  # 403 — nothing between these two lines

        async with transaction(self._pool) as tx:
            await PublishWriter(tx).delete_target(website_id)

    async def list_publications(self, website_id: UUID) -> list[PublicationResponse]:
        """This website's publication history, newest first. Unscoped, like every read."""
        rows = await PublishReader(self._pool).list_publications(website_id, MAX_PUBLICATIONS_PAGE)
        return [PublicationResponse(**row) for row in rows]

    # -----------------------------------------------------------------------------------
    # The worker's publish step
    # -----------------------------------------------------------------------------------

    async def publish_run(
        self,
        *,
        run_id: UUID,
        llms_txt: str,
        stats: dict[str, Any] | None,
        client: httpx.AsyncClient,
    ) -> PublicationResponse | None:
        """Publish one run's artifact, if this website is configured to publish at all.

        **Never raises.** Every failure becomes a `failed` publication row and a log line; see the
        module docstring for why a delivery failure must not fail a run whose artifact is already
        stored and already correct.

        Args:
            run_id: The run being published. Also the idempotency key, with its target, and the
                only identifier this method needs — `get_target_by_run` joins through `runs` to
                find the target, so a caller holding a run id does not have to also carry a
                `website_id` it has no other use for.
            llms_txt: The artifact, exactly as stored in `runs.llms_txt`.
            stats: The run's `runs.stats`. Read only by `internals/change_summary.py`, which
                decides from `index_diff` whether the index changed and writes the one line the
                commit message and pull request body carry. Nothing else in this module reads it,
                and it is passed as the raw blob rather than as two derived arguments so the
                "when in doubt, publish" bias lives in one place instead of at every call site.
            client: The caller's `httpx.AsyncClient`.

        Returns:
            The publication, or `None` when this website has no active target — which is the
            common case and is not an outcome worth recording.
        """
        if not settings.github_publish_enabled:
            return None

        reader = PublishReader(self._pool)
        target = await reader.get_target_by_run(run_id)
        if target is None or not target["active"]:
            return None

        target_id: UUID = target["id"]

        settled = await reader.find_settled_publication(run_id, target_id)
        if settled is not None:
            # A retried job. Returning the existing row rather than republishing is what keeps one
            # run from producing two pull requests.
            logger.info(
                "publish: run already published, skipping",
                extra={"run_id": str(run_id), "status": settled["status"]},
            )
            # `find_settled_publication` selects the full response shape, so the existing row is
            # returned directly rather than re-read.
            return PublicationResponse(**settled)

        # The cheap check, before any network call: a daily schedule over a site that has not
        # changed should cost nothing and commit nothing. A HINT rather than the decision — the
        # repository's own copy is compared in `_publish_to_github` too, because the repo can
        # differ from our previous run (someone edited the file by hand, or the target was just
        # pointed at a fresh repository).
        if not index_changed(stats):
            return await self._record(run_id, target_id, status="skipped_unchanged", error=None)

        try:
            return await self._publish_to_github(
                target=target,
                run_id=run_id,
                target_id=target_id,
                llms_txt=llms_txt,
                change_summary=change_summary(stats),
                client=client,
            )
        except Exception as exc:
            # Deliberately broad, and deliberately NOT bare. `except Exception` does not catch
            # `asyncio.CancelledError` (it inherits from `BaseException` since 3.8), which is
            # exactly right: a cancelled worker job must stay cancelled, and swallowing that here
            # would turn a shutdown into a hung task recorded as a publish failure.
            #
            # Every other exception — GitHub declining the write, a protected branch, a network
            # blip, a bug in this module — becomes a row the user can read, because the run itself
            # succeeded and must stay succeeded.
            logger.warning(
                "publish: failed",
                extra={"run_id": str(run_id), "error_type": type(exc).__name__},
            )
            return await self._record(
                run_id, target_id, status="failed", error=str(exc)[:_ERROR_COLUMN_LIMIT]
            )

    async def _publish_to_github(
        self,
        *,
        target: dict[str, Any],
        run_id: UUID,
        target_id: UUID,
        llms_txt: str,
        change_summary: str,
        client: httpx.AsyncClient,
    ) -> PublicationResponse:
        """The GitHub half of a publication. No transaction is open anywhere in here."""
        github_id = int(target["github_installation_id"])
        owner, repo = target["repo_owner"], target["repo_name"]
        base, path, mode = target["base_branch"], target["path"], target["mode"]

        try:
            token = await installation_token(client, github_id)
        except GitHubAuthError:
            forget_installation_token(github_id)
            raise

        # Compare against what the REPOSITORY has, not only against our previous run. The two can
        # disagree: someone may have edited the file by hand, or the target may have just been
        # pointed at a repository that has never seen this artifact. `index_changed` got us this
        # far cheaply; this is the check that decides.
        existing = await read_file(client, token.token, owner=owner, repo=repo, path=path, ref=base)
        if existing.text is not None and existing.text == llms_txt:
            return await self._record(run_id, target_id, status="skipped_unchanged", error=None)

        message = f"Update {path}\n\n{change_summary}"

        if mode == "commit":
            commit_sha = await write_file(
                client,
                token.token,
                owner=owner,
                repo=repo,
                path=path,
                branch=base,
                text=llms_txt,
                message=message,
                sha=existing.sha,
            )
            return await self._record(run_id, target_id, status="succeeded", commit_sha=commit_sha)

        # `pull_request`. The branch name carries the run id, which makes it unique per run and
        # makes a retried job converge on the same branch rather than opening a second one
        # (`create_branch` treats "already exists" as success).
        branch = f"llms-text/update-{run_id}"
        head_sha = await branch_head_sha(client, token.token, owner=owner, repo=repo, branch=base)
        await create_branch(
            client, token.token, owner=owner, repo=repo, branch=branch, from_sha=head_sha
        )

        # Re-read on the NEW branch: the blob sha `PUT /contents` needs is per-branch, and passing
        # the one read from `base` is the mistake `internals/github_client.py`'s docstring warns
        # about. It happens to be identical here — the branch was just cut from base — and reading
        # it again is what makes that a fact rather than an assumption.
        on_branch = await read_file(
            client, token.token, owner=owner, repo=repo, path=path, ref=branch
        )
        commit_sha = await write_file(
            client,
            token.token,
            owner=owner,
            repo=repo,
            path=path,
            branch=branch,
            text=llms_txt,
            message=message,
            sha=on_branch.sha,
        )
        number, url = await create_pull_request(
            client,
            token.token,
            owner=owner,
            repo=repo,
            head=branch,
            base=base,
            title=f"Update {path}",
            body=_pull_request_body(change_summary, run_id),
        )
        return await self._record(
            run_id,
            target_id,
            status="succeeded",
            commit_sha=commit_sha,
            pr_url=url,
            pr_number=number,
        )

    async def _record(
        self,
        run_id: UUID,
        target_id: UUID,
        *,
        status: str,
        commit_sha: str | None = None,
        pr_url: str | None = None,
        pr_number: int | None = None,
        error: str | None = None,
    ) -> PublicationResponse:
        """Write one publication row in its own short transaction."""
        async with transaction(self._pool) as tx:
            row = await PublishWriter(tx).record_publication(
                run_id,
                target_id,
                status=status,
                commit_sha=commit_sha,
                pr_url=pr_url,
                pr_number=pr_number,
                error=error,
            )
        return PublicationResponse(**row)

    def _require_publishing_enabled(self) -> None:
        """Refuse a configuration request when this deployment has no GitHub App.

        A `503` rather than a `404`, and the distinction is deliberate: the endpoint exists and the
        request was well-formed, but the capability is switched off for the whole deployment. That
        is what `github_publish_enabled` being `False` means, and telling a client "not
        implemented" would be a lie about a feature that is merely unconfigured.
        """
        if not settings.github_publish_enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Publishing to GitHub is not enabled for this deployment.",
            )


def _to_target(row: dict[str, Any]) -> PublishTargetResponse:
    """One joined `publish_targets` row as the API returns it."""
    return PublishTargetResponse(
        id=row["id"],
        website_id=row["website_id"],
        installation_id=row["installation_row_id"],
        account_login=row["account_login"],
        repo_owner=row["repo_owner"],
        repo_name=row["repo_name"],
        base_branch=row["base_branch"],
        path=row["path"],
        mode=row["mode"],
        active=row["active"],
    )


def _pull_request_body(change_summary: str, run_id: UUID) -> str:
    """The pull request body: what changed, and where it came from.

    Deliberately plain and short. This lands in someone's repository, written by a bot, and the
    two things a reviewer wants are "why am I looking at this" and "what produced it". Anything
    more would be a template to maintain.
    """
    return (
        f"{change_summary}\n\n"
        "This pull request was opened automatically by [llms-text](https://llmstxt.org) after a "
        "crawl of this site found the generated `llms.txt` had changed.\n\n"
        f"Run `{run_id}`."
    )
