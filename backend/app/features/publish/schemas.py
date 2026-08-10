"""Request and response shapes for publishing an artifact to a GitHub repository.

Every field a client sends is validated here rather than in a router or a service, the same
division `features/websites/schemas.py` draws: a router parses, a service decides, and this
module is what makes "parses" mean something. The repository coordinates in particular are
user-supplied strings that end up interpolated into a GitHub API path, so their constraints are
load-bearing rather than cosmetic — see `_REPO_SEGMENT` and `PublishTargetRequest.path`.
"""

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


PublishMode = Literal["pull_request", "commit"]
"""Mirrors the `publish_mode` Postgres enum. A `Literal` rather than a Python `Enum` for the
same reason `RunStatus` is spelled this way in `features/runs/schemas.py`: the value crosses the
wire as a string, and a `Literal` is what makes the OpenAPI schema say so."""

PublishStatus = Literal["skipped_unchanged", "succeeded", "failed"]

_REPO_SEGMENT: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
"""What an owner or repository name may contain.

GitHub's own rule, and a security boundary rather than a nicety: both values are interpolated
into a request path in `internals/github_client.py`, so a segment containing `/` or `..` would
let a caller address a different endpoint than the one the code reads as. Anchored at both ends,
and required to start alphanumeric, which excludes a leading `.` or `-`.
"""

_BRANCH_PATTERN: Final = r"^[^\s~^:?*\[\\]+$"
"""A git ref name, by exclusion rather than by enumeration — git's own rules forbid whitespace
and this set of metacharacters. Deliberately permissive about everything else: branch naming
conventions vary wildly and rejecting a legal name would be a bug the user cannot work around."""


class InstallationResponse(BaseModel):
    """One GitHub App installation this user has connected."""

    id: UUID
    installation_id: int = Field(
        description="GitHub's own installation id. Not a secret — it grants nothing without "
        "this deployment's App private key."
    )
    account_login: str = Field(
        description="The GitHub account or organization it was installed on."
    )
    created_at: datetime


class RepositoryResponse(BaseModel):
    """One repository an installation can write to, for the target picker."""

    owner: str
    name: str
    default_branch: str
    private: bool


class RepositoryListResponse(BaseModel):
    """`GET /github/installations/{id}/repositories`."""

    repositories: list[RepositoryResponse]
    truncated: bool = Field(
        description="Whether the installation has more repositories than this list carries "
        "(`MAX_REPOSITORIES`). True means enter the repository by name rather than pick it."
    )


class PublishTargetRequest(BaseModel):
    """`PUT /websites/{id}/publish-target` — where this site's `llms.txt` should go."""

    installation_id: UUID = Field(
        description="The `id` of one of this user's own `GET /github/installations` rows — this "
        "API's uuid, not GitHub's numeric installation id."
    )
    repo_owner: Annotated[str, Field(min_length=1, max_length=100, pattern=_REPO_SEGMENT)]
    repo_name: Annotated[str, Field(min_length=1, max_length=100, pattern=_REPO_SEGMENT)]
    base_branch: Annotated[str, Field(min_length=1, max_length=255, pattern=_BRANCH_PATTERN)]
    path: Annotated[str, Field(min_length=1, max_length=255)] = "llms.txt"
    mode: PublishMode = "pull_request"
    active: bool = Field(
        default=False,
        description="Whether successful runs publish to this target. Off by default: connecting "
        "a repository and authorizing writes to it are two separate decisions.",
    )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        """A repository-relative file path, normalized and constrained.

        Three rejections, and each is a real failure rather than a style preference:

        * **Absolute paths and `..` segments.** `internals/github_client.py` interpolates this
          into a Contents API path; `..` there is a path-traversal attempt, and while GitHub
          would reject it, this API should not be the thing relying on that.
        * **A trailing slash**, which names a directory. The Contents API answers a directory
          with a JSON array, which the client already reports as an error — but rejecting it here
          gives the user a `422` at the moment they configure it rather than a failed publication
          hours later.
        * **A backslash**, because a Windows-style path in a git repository is a filename
          containing a backslash, which is virtually never what the user meant.
        """
        cleaned = value.strip().lstrip("/")
        if not cleaned:
            raise ValueError("path must name a file, such as llms.txt")
        if cleaned.endswith("/"):
            raise ValueError("path must name a file, not a directory")
        if "\\" in cleaned:
            raise ValueError("path must use forward slashes")
        if any(segment == ".." for segment in cleaned.split("/")):
            raise ValueError("path must not contain '..'")
        return cleaned


class PublishTargetResponse(BaseModel):
    """A website's publish target, as the API returns it."""

    id: UUID
    website_id: UUID
    installation_id: UUID
    account_login: str = Field(
        description="Denormalized from the installation, so a client rendering the target does "
        "not need a second request to name the account it publishes as."
    )
    repo_owner: str
    repo_name: str
    base_branch: str
    path: str
    mode: PublishMode
    active: bool


class PublicationResponse(BaseModel):
    """One publication attempt.

    `status` is the field worth reading first: `skipped_unchanged` is a success in the sense that
    matters — the system looked, the index was identical, and nothing needed doing. A history of
    those is a working schedule over a stable site, not a broken one.
    """

    id: UUID
    run_id: UUID
    status: PublishStatus
    commit_sha: str | None
    pr_url: str | None
    pr_number: int | None
    error: str | None
    created_at: datetime
