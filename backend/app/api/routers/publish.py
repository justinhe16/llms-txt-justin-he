"""HTTP handlers for GitHub installations and publish targets. Thin by contract (§3.2).

Every handler parses its input, calls exactly one service method, and returns the result — no
`if`, no `for`, no SQL, mirroring `api/routers/schedules.py`.

**Authentication vs. authorization, visible in the signatures**, the same division
`schedules.py` states: every handler takes `CurrentUserId`, so every endpoint is `401` without a
token. The installation handlers pass it on because an installation belongs to a user. The two
website-scoped READS (`get_publish_target`, `list_publications`) take it and deliberately ignore
it — reads are authenticated and unscoped (§4.1) — while the two website-scoped WRITES pass it
through for `require_owner`.

**Route naming.** `/github/installations` is not a plural noun under `/websites`, and that is
deliberate: an installation is not a sub-resource of a website. The website-scoped routes stay in
the established shape, `/websites/{id}/publish-target` beside `/websites/{id}/schedule`.
"""

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUserId, DbPool
from app.features.publish.http_client import build_github_client
from app.features.publish.schemas import (
    InstallationResponse,
    PublicationResponse,
    PublishTargetRequest,
    PublishTargetResponse,
    RepositoryListResponse,
)
from app.features.publish.service import PublishService
from app.features.websites.service import WebsiteService


router = APIRouter(tags=["publish"])


def get_publish_service(pool: DbPool) -> PublishService:
    """Build the service for one request from the process-wide pool.

    Feature-specific wiring beside the feature's routes, exactly like `get_schedule_service`.
    """
    return PublishService(pool, WebsiteService(pool))


async def get_github_client() -> AsyncIterator[httpx.AsyncClient]:
    """One `httpx.AsyncClient` per request, closed when the request ends.

    A generator dependency rather than a module-level client, which is §3.7's rule and not a
    preference: a client owns a connection pool, and a singleton would outlive the event loop in
    tests and leak sockets in production. `async with` here is what guarantees `aclose()` runs
    even when a handler raises.
    """
    async with build_github_client() as client:
        yield client


PublishServiceDep = Annotated[PublishService, Depends(get_publish_service)]
GitHubClientDep = Annotated[httpx.AsyncClient, Depends(get_github_client)]


@router.get("/github/installations", response_model=list[InstallationResponse])
async def list_installations(
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> list[InstallationResponse]:
    """Every GitHub App installation the caller has connected.

    Per-user, unlike every other read in this API. An installation is a fact about someone's
    GitHub account rather than about a crawled site, which is the distinction
    `internals/publish_reader.py`'s docstring draws against §4.1.
    """
    return await service.list_installations(user_id)


@router.post(
    "/github/installations",
    response_model=InstallationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def connect_installation(
    installation_id: int,
    user_id: CurrentUserId,
    service: PublishServiceDep,
    client: GitHubClientDep,
) -> InstallationResponse:
    """Record an installation the caller was redirected back from.

    `installation_id` is GitHub's own numeric id, arriving from the setup callback's query string.
    The service verifies it against GitHub before writing anything — see
    `PublishService.connect_installation`, because a number on a redirect is not a fact.

    Always `201`, including when the row already existed and was refreshed. Distinguishing the two
    would need a branch in this handler, which is what CLAUDE.md #6 forbids; the same reasoning
    `upsert_schedule` gives for always returning `200`.
    """
    return await service.connect_installation(installation_id, user_id, client)


@router.delete("/github/installations/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_installation(
    id: UUID,
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> None:
    """Forget an installation and every publish target that used it.

    Does not uninstall the App on GitHub — that is the user's own action to take in their account,
    and `PublishService.disconnect_installation` explains why this API does not attempt it.
    """
    await service.disconnect_installation(id, user_id)


@router.get("/github/installations/{id}/repositories", response_model=RepositoryListResponse)
async def list_installation_repositories(
    id: UUID,
    user_id: CurrentUserId,
    service: PublishServiceDep,
    client: GitHubClientDep,
) -> RepositoryListResponse:
    """The repositories an installation may write to, for the target picker.

    Proxied live from GitHub rather than stored: the set changes whenever the user edits the
    installation's repository access, and a cached copy would offer a repository we can no longer
    write to. `502` when GitHub cannot be reached — the request was fine, the upstream was not.
    """
    return await service.list_installation_repositories(id, user_id, client)


@router.get("/websites/{id}/publish-target", response_model=PublishTargetResponse | None)
async def get_publish_target(
    id: UUID,
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> PublishTargetResponse | None:
    """Return where a website publishes, or `null`.

    `200` with a `null` body when the website has no target — a normal state, not an error, exactly
    as `get_schedule` treats a website with no schedule.
    """
    return await service.get_target(id)


@router.put("/websites/{id}/publish-target", response_model=PublishTargetResponse)
async def upsert_publish_target(
    id: UUID,
    body: PublishTargetRequest,
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> PublishTargetResponse:
    """Create or replace where a website the caller owns publishes.

    `403` when the caller is not the owner, `404` when `installation_id` names no installation of
    theirs — the second is a `404` rather than a `403` because telling a caller that an
    installation exists but belongs to someone else would leak which GitHub accounts other users
    have connected.
    """
    return await service.upsert_target(id, body, user_id)


@router.delete("/websites/{id}/publish-target", status_code=status.HTTP_204_NO_CONTENT)
async def delete_publish_target(
    id: UUID,
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> None:
    """Stop publishing a website the caller owns. Idempotent — `204` even if there was none."""
    await service.delete_target(id, user_id)


@router.get("/websites/{id}/publications", response_model=list[PublicationResponse])
async def list_publications(
    id: UUID,
    user_id: CurrentUserId,
    service: PublishServiceDep,
) -> list[PublicationResponse]:
    """A website's publication history, newest first, capped at `MAX_PUBLICATIONS_PAGE`.

    Not paginated, and not `Page[...]`: this is a panel on a website's page whose list is almost
    always length zero or one. See `MAX_PUBLICATIONS_PAGE` for the argument.
    """
    return await service.list_publications(id)
