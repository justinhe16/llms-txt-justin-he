"""Turning this deployment's GitHub App private key into a short-lived installation token.

The whole credential story for publishing lives in this module, and it is worth stating up
front what it deliberately does NOT do: **nothing here ever persists a credential.** The
`github_installations` table stores an installation id and an account name; it stores no token,
no refresh token, and no key. Every write to a user's repository is authorized by a token minted
here, held in memory, and allowed to expire.

That is the reason this feature is a GitHub App rather than an OAuth token or a user-supplied
PAT, and the reason is architectural rather than stylistic:

* **A publish happens in the WORKER, hours after the user's browser closed.** A scheduled run
  has no session, no cookie, and no `provider_token` — Supabase returns one only on the initial
  OAuth exchange and does not persist it. Any design that authorizes a repo write with a
  browser-obtained token cannot do the one thing this feature exists for.
* **A database dump grants nobody write access to anybody's repository.** A stored PAT would;
  an installation id does not. The blast radius of the row is "we know you installed our App".
* **Revocation is the user's, and is immediate.** Uninstalling the App in GitHub's own UI stops
  every future token from being issued, with nothing for this codebase to notice or clean up.

## Two tokens, and they are not interchangeable

1. **The App JWT** (`mint_app_jwt`) — signed locally with the private key, RS256, ten minutes.
   It authenticates *the App itself*, and is accepted on exactly two kinds of endpoint: the
   `/app/...` ones, and the token exchange below. It CANNOT read or write a repository, which
   is why passing it to the Contents API produces a puzzling 403 rather than a clear error.
2. **The installation access token** (`installation_token`) — obtained by presenting the App JWT
   to `POST /app/installations/{id}/access_tokens`, about an hour, scoped to the repositories
   that installation was granted. This is the one every actual publish uses.

Getting these the wrong way round is the single most likely mistake in this file's subject
matter, so each function's return type is named for which one it is.

## The cache, and why it is per-process rather than in Redis

`_TOKEN_CACHE` is a module-level dict, so each API and worker process keeps its own. A shared
cache in Redis would save a token exchange or two and cost a new failure mode — a stale token
readable by every process, and a serialization format to version — for a call that takes one
round trip and happens at most once every `github_token_ttl_s` per installation. The token is
also a live credential, and keeping it out of any datastore is the property this whole module is
organized around; putting it in Redis would trade that away for a rounding error in latency.

The cache is keyed by installation id and holds an expiry this module computes, deliberately
shorter than the one GitHub sends. See `InstallationToken`.
"""

import time
from dataclasses import dataclass
from typing import Final

import httpx
import jwt

from app.core.settings import settings


GITHUB_API_BASE: Final = "https://api.github.com"

_JWT_ALGORITHM: Final = "RS256"
"""The only algorithm GitHub accepts for an App JWT, and the only one this module signs with.

Pinned as a constant rather than passed by a caller for the same reason
`app/core/auth/dependencies.py` pins its own verification algorithms: an algorithm chosen
anywhere but the code is an algorithm an attacker can influence. There is no verification here
— we are the signer — but the habit is worth keeping in a file that handles a private key.
"""

_JWT_MAX_WINDOW_S: Final = 600
"""GitHub's hard ceiling on `exp - iat` for an App JWT: ten minutes. Not a value to tune."""

_JWT_LIFETIME_S: Final = 480
"""How far ahead of NOW an App JWT's `exp` is set: eight minutes.

**The number that has to stay under `_JWT_MAX_WINDOW_S` is `_JWT_LIFETIME_S + _JWT_BACKDATE_S`,
not this constant alone**, and that is the whole reason this docstring exists. `iat` is backdated
by `_JWT_BACKDATE_S`, so the window GitHub measures is the SUM of the two — a "nine minute"
lifetime with a one-minute backdate is a ten-minute window, which is exactly the boundary that
fails whenever our clock is a second ahead of GitHub's. 480 + 60 = 540 leaves a full minute of
margin on both sides.

`test_mint_app_jwt_signs_claims_github_accepts` asserts the sum rather than this constant, which is
what makes the invariant hold if either number is ever changed on its own.
"""

_JWT_BACKDATE_S: Final = 60
"""How far `iat` is backdated.

GitHub's own documentation recommends this, and the reason is the mirror of the above: a JWT
whose `iat` is in GitHub's future is rejected outright, and a laptop or container clock running
a few seconds fast is entirely ordinary. Sixty seconds of slack costs nothing — the token is
still short-lived — and removes a whole category of "works on my machine".
"""

_API_VERSION_HEADER: Final = "2022-11-28"
"""The `X-GitHub-Api-Version` every request here sends. Named once because two functions in this
module send it, and `internals/github_client.py` pins the same value for its own requests — a
version that drifted between the token exchange and the writes it authorizes would be a confusing
failure to diagnose."""

_TOKEN_REQUEST_TIMEOUT_S: Final = 15.0
"""Timeout for the token exchange. Its own number rather than a crawl timeout: this is one small
request to GitHub's API, not a page fetch from an arbitrary site, and it sits inside a worker job
whose overall budget is `JOB_TIMEOUT_SECONDS` (600s, `app/worker/policy.py`)."""


class GitHubAuthError(RuntimeError):
    """Minting or exchanging a credential failed.

    Raised instead of letting an `httpx` or `jwt` exception escape, so callers have one thing to
    catch for "we could not get a token" and can distinguish it from a publish that failed for a
    repository-shaped reason (a protected branch, a deleted repo).

    **Its message never contains a token, a key, or a key fragment.** A GitHub error body can
    echo request details, so `installation_token` below quotes only the status code and
    GitHub's own `message` field, never the raw body.
    """


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """An installation access token and the moment this process stops reusing it.

    A distinct type from `str` so a function that needs the installation token cannot be handed
    an App JWT — the two are both `str` on the wire and are accepted on disjoint sets of
    endpoints (see the module docstring), which makes them exactly the pair worth making the
    type system tell apart.
    """

    token: str
    expires_at: float

    def __repr__(self) -> str:
        """Redacted, so a token cannot reach a log through an exception traceback, a `repr()` in
        a debugger, or a dataclass printed into a log record's `extra`. Dataclasses generate a
        `__repr__` that prints every field, and this one holds a live write credential."""
        return f"InstallationToken(token='***', expires_at={self.expires_at})"


_TOKEN_CACHE: dict[int, InstallationToken] = {}


def mint_app_jwt() -> str:
    """Sign a short-lived JWT authenticating the App itself.

    Reads `settings.github_app_private_key` and `settings.github_app_id`. Neither value appears
    in the return value, in an exception message, or in a log line from this function.

    Returns:
        The encoded JWT. Authenticates the APP — not an installation — so it is accepted by
        `POST /app/installations/{id}/access_tokens` and by the `/app/...` endpoints, and by
        nothing that reads or writes repository contents.

    Raises:
        GitHubAuthError: The key is missing, or is not a PEM private key PyJWT can sign with.
    """
    key = settings.github_app_private_key.strip()
    if not key:
        raise GitHubAuthError(
            "GITHUB_APP_PRIVATE_KEY is not set, so no GitHub App JWT can be signed."
        )
    if not settings.github_app_id.strip():
        raise GitHubAuthError("GITHUB_APP_ID is not set, so no GitHub App JWT can be signed.")

    # Fly secrets set through a shell frequently arrive with literal backslash-n instead of real
    # newlines, and a PEM with no line breaks is not a PEM — `cryptography` rejects it with a
    # message that says nothing about newlines. Normalizing here, at the one place the key is
    # read, means the deployment step does not have to be careful about quoting.
    pem = key.replace("\\n", "\n")

    now = int(time.time())
    try:
        return jwt.encode(
            {
                "iat": now - _JWT_BACKDATE_S,
                "exp": now + _JWT_LIFETIME_S,
                "iss": settings.github_app_id.strip(),
            },
            pem,
            algorithm=_JWT_ALGORITHM,
        )
    except Exception as exc:
        # Deliberately broad: PyJWT and `cryptography` raise several unrelated exception types
        # for a malformed key, and every one of them means the same thing to a caller. The
        # exception's own text is NOT included — a key-parsing error can quote the input.
        raise GitHubAuthError(
            f"Could not sign a GitHub App JWT ({type(exc).__name__}). Check that "
            "GITHUB_APP_PRIVATE_KEY holds the App's PEM private key."
        ) from None


async def installation_token(client: httpx.AsyncClient, installation_id: int) -> InstallationToken:
    """The installation access token for `installation_id`, from cache or freshly exchanged.

    Args:
        client: The caller's own `httpx.AsyncClient`. Passed in rather than constructed here for
            the same reason `app/infrastructure/storage/` takes one: a module-level client is a
            singleton with a connection pool and a lifecycle, and this codebase has decided
            against those (ARCHITECTURE.md §3.7).
        installation_id: GitHub's own installation id, from `github_installations`.

    Returns:
        A token valid for at least a few minutes.

    Raises:
        GitHubAuthError: The key could not be signed with, or GitHub refused the exchange —
            which is what a revoked or uninstalled App looks like from here (`404`).
    """
    cached = _TOKEN_CACHE.get(installation_id)
    if cached is not None and cached.expires_at > time.monotonic():
        return cached

    app_jwt = mint_app_jwt()
    try:
        response = await client.post(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION_HEADER,
            },
            timeout=_TOKEN_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise GitHubAuthError(
            f"Could not reach GitHub to exchange an installation token ({type(exc).__name__})."
        ) from None

    if response.status_code != httpx.codes.CREATED:
        # `404` here is the interesting one and the most common: it is what GitHub returns for an
        # installation that no longer exists, which is to say a user who uninstalled the App.
        # That is a legitimate end state rather than an outage, and the caller surfaces it to the
        # user as "reconnect the repository" rather than retrying it.
        raise GitHubAuthError(
            f"GitHub refused to issue an installation token (HTTP {response.status_code}: "
            f"{github_message(response)}). If this is a 404, the App may have been uninstalled."
        )

    token = response.json().get("token")
    if not isinstance(token, str) or not token:
        raise GitHubAuthError("GitHub's token response contained no token.")

    # `monotonic`, not `time.time`, for the expiry this process enforces: a wall-clock jump —
    # NTP correcting a drifted container — would otherwise make a live token look expired or, far
    # worse, an expired one look live. GitHub's own `expires_at` is deliberately not parsed and
    # used here: it is a wall-clock instant from another machine, and `github_token_ttl_s`
    # already sits well inside it.
    minted = InstallationToken(
        token=token, expires_at=time.monotonic() + settings.github_token_ttl_s
    )
    _TOKEN_CACHE[installation_id] = minted
    return minted


def forget_installation_token(installation_id: int) -> None:
    """Drop a cached token, so the next publish mints a fresh one.

    Called when GitHub rejects a token this process believed was good — a `401` on a request the
    cache said was authorized. Without this, one revoked token would keep being presented for the
    rest of `github_token_ttl_s` and every publish in that window would fail identically.
    """
    _TOKEN_CACHE.pop(installation_id, None)


def github_message(response: httpx.Response) -> str:
    """GitHub's own `message` field, or a bounded fallback.

    Never the raw body. An error body can echo parts of the request, and this text ends up in a
    `publications.error` column and in a log line; `message` is the one field GitHub documents as
    human-readable, and capping it keeps a pathological response from filling the column.
    """
    try:
        message = response.json().get("message")
    except ValueError:
        return "unreadable response body"
    return str(message)[:200] if message else "no message"


async def fetch_installation_account(client: httpx.AsyncClient, installation_id: int) -> str:
    """The account login a given installation belongs to — `justinhe16`, or an org name.

    Uses the APP JWT rather than an installation token, because this is an `/app/...` endpoint
    (see the module docstring's "Two tokens" section). That also makes it the natural place to
    VERIFY an installation id before recording it: GitHub's setup callback hands us
    `installation_id` as a query parameter on a redirect, which is to say an untrusted number
    from the user's browser. Asking GitHub what that installation is — with a credential only
    this deployment holds — is what turns it into a fact.

    Raises:
        GitHubAuthError: The id names no installation of this App (`404`), or the request failed.
            A `404` is the important case: it is what an id somebody typed into the URL bar looks
            like, and it must not become a row in `github_installations`.
    """
    app_jwt = mint_app_jwt()
    try:
        response = await client.get(
            f"{GITHUB_API_BASE}/app/installations/{installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _API_VERSION_HEADER,
            },
            timeout=_TOKEN_REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise GitHubAuthError(
            f"Could not reach GitHub to verify the installation ({type(exc).__name__})."
        ) from None

    if response.status_code != httpx.codes.OK:
        raise GitHubAuthError(
            f"GitHub does not recognize installation {installation_id} for this App "
            f"(HTTP {response.status_code}: {github_message(response)})."
        )

    account = response.json().get("account") or {}
    login = account.get("login")
    if not isinstance(login, str) or not login:
        # An installation on a user or org always has an account with a login. Guarded anyway
        # because this value is stored and displayed, and an empty account name in the UI reads
        # as a bug in the page rather than as a surprising GitHub response.
        raise GitHubAuthError(
            f"GitHub returned installation {installation_id} with no account login."
        )
    return login
