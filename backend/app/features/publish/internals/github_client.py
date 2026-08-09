"""The GitHub REST calls one publication needs, and nothing else.

A thin, explicit client over `httpx` rather than a GitHub SDK — the same call this repository
already made for Supabase Storage, and for the same reason: the six endpoints below are the whole
surface this feature touches, each is one request with a documented shape, and a dependency whose
release notes we would have to follow buys nothing at that size. (Contrast
`app/features/crawl/anthropic_client.py`, where the official SDK IS used: streaming, retries and
token accounting are real machinery worth importing. There is none of that here.)

Every function takes the installation token as an argument. None of them mints one, caches one, or
reads a setting — `internals/github_app.py` owns credentials and this module owns HTTP, so a test
can drive every call below with a fake token and no key present at all.

## Why the Contents API rather than the Git Data API

Writing a file through `PUT /repos/{owner}/{repo}/contents/{path}` is one request that creates a
blob, a tree and a commit together. The Git Data equivalent is four (blob, tree, commit, ref) and
buys the ability to write several files in one commit — which this feature will never do, because
it publishes exactly one file. One request also means no partially-written state to reason about
when the second of four calls fails.

The one place the Git Data API is still needed is creating the pull request's branch, because
`POST /git/refs` is the only way to make a ref pointing at an existing commit.

## Conditional writes, and the sha that is easy to get wrong

`PUT /contents` requires the CURRENT blob sha when overwriting a file, and rejects the write with
`409` if it does not match. That is optimistic concurrency, and it is what makes a publication
safe against a human editing `llms.txt` between our read and our write.

The subtlety: that sha is per-BRANCH. A file's blob sha on `main` is not its sha on the branch we
just cut, unless nothing changed — which is true here only because we cut the branch from `main`
moments earlier. `read_file` is therefore always called against the branch that is about to be
written, never against a different one.
"""

import base64
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

from app.features.publish.internals.github_app import GITHUB_API_BASE, github_message


_REQUEST_TIMEOUT_S: Final = 20.0
"""Per-request timeout. Larger than the token exchange's because a Contents write carries the
whole artifact as a body, and smaller than the worker's job budget by a wide margin."""

_ACCEPT: Final = "application/vnd.github+json"
_API_VERSION: Final = "2022-11-28"

MAX_REPOSITORIES: Final = 100
"""How many repositories the picker lists — GitHub's own per-page maximum, taken in one request.

Deliberately not paginated. An installation granted more than 100 repositories is a case this UI
answers with a text field rather than a longer list, and `repositories_truncated` on the response
is what tells the frontend to say so instead of silently showing a prefix.
"""


class GitHubApiError(RuntimeError):
    """A GitHub request failed for a repository-shaped reason.

    Distinct from `GitHubAuthError` on purpose: that one means "we could not get a credential"
    and is usually the deployment's problem, while this one means "GitHub declined this
    operation" — a protected branch, a deleted repository, a path that is a directory — and is
    usually the user's to fix. The two get different copy in the UI and different retry
    behaviour, so they are different types.
    """


@dataclass(frozen=True, slots=True)
class RepoFile:
    """One file as it currently exists on a branch, or its absence.

    `sha` is the BLOB sha needed to overwrite it, and `None` together with `text is None` means
    the path does not exist yet — which is a normal first publication, not an error.
    """

    text: str | None
    sha: str | None

    @property
    def exists(self) -> bool:
        return self.sha is not None


@dataclass(frozen=True, slots=True)
class Repository:
    """One repository an installation can write to, for the target picker."""

    owner: str
    name: str
    default_branch: str
    private: bool


async def list_repositories(client: httpx.AsyncClient, token: str) -> tuple[list[Repository], bool]:
    """Every repository this installation was granted, and whether the list was truncated.

    Returns:
        `(repositories, truncated)` — `truncated` is `True` when the installation has more than
        `MAX_REPOSITORIES`, so the caller can say so rather than presenting a prefix as the whole.
    """
    payload = await _request(
        client,
        token,
        "GET",
        f"{GITHUB_API_BASE}/installation/repositories?per_page={MAX_REPOSITORIES}",
    )
    raw = payload.get("repositories") or []
    total = payload.get("total_count")

    repositories = [
        Repository(
            owner=(entry.get("owner") or {}).get("login", ""),
            name=entry.get("name", ""),
            default_branch=entry.get("default_branch") or "main",
            private=bool(entry.get("private")),
        )
        for entry in raw
        if isinstance(entry, dict) and entry.get("name")
    ]
    truncated = isinstance(total, int) and total > len(repositories)
    return repositories, truncated


async def read_file(
    client: httpx.AsyncClient, token: str, *, owner: str, repo: str, path: str, ref: str
) -> RepoFile:
    """The file at `path` on `ref`, or an empty `RepoFile` if it does not exist.

    A `404` is returned as absence rather than raised, because "this repository has no llms.txt
    yet" is the expected state of a first publication and forcing every caller to catch an
    exception for it would make the normal path the exceptional one.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{_quote_path(path)}?ref={ref}"
    response = await _send(client, token, "GET", url)

    if response.status_code == httpx.codes.NOT_FOUND:
        return RepoFile(text=None, sha=None)
    if response.status_code != httpx.codes.OK:
        raise GitHubApiError(
            f"Could not read {path} from {owner}/{repo}@{ref} "
            f"(HTTP {response.status_code}: {github_message(response)})."
        )

    payload = response.json()
    if isinstance(payload, list):
        # The path names a directory. Worth its own message: the user typed a path, and "expected
        # a file" is actionable where a JSON decode error is not.
        raise GitHubApiError(
            f"{path} in {owner}/{repo} is a directory, not a file. Set the publish path to a "
            "file name such as llms.txt."
        )

    encoding = payload.get("encoding")
    content = payload.get("content")
    sha = payload.get("sha")

    # A file over 1 MB comes back with `encoding: "none"` and no content. We still get the sha,
    # which is all an overwrite needs — but with no text there is nothing to compare, so the
    # caller must treat the content as unknown and write unconditionally rather than concluding
    # "unchanged". `text=None` with `sha` set is exactly that state, distinct from absence.
    text: str | None = None
    if encoding == "base64" and isinstance(content, str):
        try:
            text = base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            # A repository may hold a non-UTF-8 file at this path. Not our problem to decode; the
            # publication overwrites it, and an undecodable existing file simply is not comparable.
            text = None

    return RepoFile(text=text, sha=sha if isinstance(sha, str) else None)


async def branch_head_sha(
    client: httpx.AsyncClient, token: str, *, owner: str, repo: str, branch: str
) -> str:
    """The commit sha `branch` points at — what a new branch is cut from."""
    payload = await _request(
        client,
        token,
        "GET",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/ref/heads/{branch}",
    )
    sha = (payload.get("object") or {}).get("sha")
    if not isinstance(sha, str):
        raise GitHubApiError(f"{owner}/{repo} has no branch named {branch}.")
    return sha


async def create_branch(
    client: httpx.AsyncClient, token: str, *, owner: str, repo: str, branch: str, from_sha: str
) -> None:
    """Create `branch` pointing at `from_sha`.

    A `422` from GitHub means the ref already exists. That is treated as success rather than an
    error: a retried worker job re-running the same publication should converge on the branch it
    already made, not fail because its first attempt got further than it recorded.
    """
    response = await _send(
        client,
        token,
        "POST",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": from_sha},
    )
    if response.status_code == httpx.codes.CREATED:
        return
    if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        return
    raise GitHubApiError(
        f"Could not create branch {branch} in {owner}/{repo} "
        f"(HTTP {response.status_code}: {github_message(response)})."
    )


async def write_file(
    client: httpx.AsyncClient,
    token: str,
    *,
    owner: str,
    repo: str,
    path: str,
    branch: str,
    text: str,
    message: str,
    sha: str | None,
) -> str:
    """Create or update `path` on `branch`, returning the new commit's sha.

    Args:
        sha: The current blob sha at `path` ON `branch`, or `None` to create the file. Getting
            this from a different branch is the mistake this module's docstring warns about — a
            mismatch is a `409`, not a silent overwrite, which is the whole point of sending it.
        message: The commit message.

    Raises:
        GitHubApiError: Including the two cases worth naming — `409`, meaning someone changed the
            file since it was read, and `403`/`422` on a protected branch.
    """
    body: dict[str, Any] = {
        "message": message,
        # `.decode()` because the API wants a JSON string and `b64encode` returns bytes. The
        # artifact is encoded as UTF-8 first, which is what makes a non-ASCII title survive.
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha is not None:
        body["sha"] = sha

    response = await _send(
        client,
        token,
        "PUT",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{_quote_path(path)}",
        json=body,
    )
    if response.status_code not in (httpx.codes.OK, httpx.codes.CREATED):
        detail = github_message(response)
        if response.status_code == httpx.codes.CONFLICT:
            detail = f"{detail} — {path} changed in the repository after it was read."
        raise GitHubApiError(
            f"Could not write {path} to {owner}/{repo}@{branch} "
            f"(HTTP {response.status_code}: {detail})."
        )

    commit_sha = (response.json().get("commit") or {}).get("sha")
    if not isinstance(commit_sha, str):
        raise GitHubApiError(f"GitHub accepted the write to {path} but returned no commit sha.")
    return commit_sha


async def create_pull_request(
    client: httpx.AsyncClient,
    token: str,
    *,
    owner: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> tuple[int, str]:
    """Open a pull request from `head` into `base`, returning `(number, html_url)`."""
    response = await _send(
        client,
        token,
        "POST",
        f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls",
        json={"title": title, "body": body, "head": head, "base": base},
    )
    if response.status_code != httpx.codes.CREATED:
        raise GitHubApiError(
            f"Could not open a pull request in {owner}/{repo} "
            f"(HTTP {response.status_code}: {github_message(response)})."
        )
    payload = response.json()
    number, url = payload.get("number"), payload.get("html_url")
    if not isinstance(number, int) or not isinstance(url, str):
        raise GitHubApiError("GitHub opened a pull request but returned no number or URL.")
    return number, url


async def _request(
    client: httpx.AsyncClient, token: str, method: str, url: str, **kwargs: Any
) -> dict[str, Any]:
    """`_send` plus "the response must be a 2xx JSON object", for the calls where anything else
    is an error rather than a state."""
    response = await _send(client, token, method, url, **kwargs)
    if not response.is_success:
        raise GitHubApiError(
            f"GitHub request failed (HTTP {response.status_code}: {github_message(response)})."
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise GitHubApiError("GitHub returned an unexpected response shape.")
    return payload


async def _send(
    client: httpx.AsyncClient, token: str, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """One authenticated request, with transport failures turned into `GitHubApiError`.

    The token goes in the `Authorization` header and nowhere else — never a query parameter,
    which would put a live write credential into GitHub's access logs and into any redirect.
    """
    try:
        return await client.request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT,
                "X-GitHub-Api-Version": _API_VERSION,
            },
            timeout=_REQUEST_TIMEOUT_S,
            **kwargs,
        )
    except httpx.HTTPError as exc:
        raise GitHubApiError(f"Could not reach GitHub ({type(exc).__name__}).") from None


def _quote_path(path: str) -> str:
    """A repository path, safe to interpolate into a URL.

    Slashes are preserved — they are path structure, not data — while spaces and other characters
    a path may legitimately contain are encoded. `urllib.parse.quote`'s default `safe="/"` is
    exactly this behaviour; it is spelled out here because the one thing that must NOT happen is
    a path segment escaping into the query string.
    """
    return quote(path.strip().lstrip("/"), safe="/")
