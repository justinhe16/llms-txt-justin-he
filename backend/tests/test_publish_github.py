"""Tests for `app.features.publish.internals.github_app` and `.github_client` — minting a
credential, and the GitHub calls one publication makes.

No network: every request is answered by an `httpx.MockTransport`, the same way
`tests/test_crawl_fetcher.py` drives the crawler's own client. No real GitHub App either — the RSA
key below is generated in-process, so this suite proves the signing path works without any secret
existing anywhere.

**Two properties are asserted repeatedly, because both are security-shaped rather than
behavioural.** First, a credential never appears in an error message or a `repr` — a token in a log
line is a token in `fly logs`, which is the one log surface this system has. Second, the token goes
in the `Authorization` header and never in a URL, because a query-string credential lands in
GitHub's access logs and in every redirect.
"""

import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.settings import settings
from app.features.publish.internals import github_app
from app.features.publish.internals.github_app import (
    GitHubAuthError,
    InstallationToken,
    fetch_installation_account,
    forget_installation_token,
    installation_token,
    mint_app_jwt,
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


_APP_ID = "123456"


@pytest.fixture(scope="module")
def rsa_pem() -> tuple[str, Any]:
    """A throwaway RSA keypair: the PEM to sign with, and the public key to verify against.

    Module-scoped because 2048-bit key generation is the slowest thing in this file by an order of
    magnitude, and every test wants the same key.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def _clear_token_cache() -> None:
    """Empty the module-level token cache before each test.

    `_TOKEN_CACHE` is process-wide by design (see the module's docstring), which makes it shared
    state between tests — one test's cached token would silently satisfy another's exchange and the
    caching test would pass for the wrong reason.
    """
    github_app._TOKEN_CACHE.clear()


@pytest.fixture
def app_configured(monkeypatch: pytest.MonkeyPatch, rsa_pem: tuple[str, Any]) -> Any:
    """Configure the deployment as if a GitHub App were registered."""
    pem, public_key = rsa_pem
    monkeypatch.setattr(settings, "github_app_id", _APP_ID)
    monkeypatch.setattr(settings, "github_app_private_key", pem)
    monkeypatch.setattr(settings, "github_token_ttl_s", 2400)
    return public_key


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------------------
# The App JWT
# ---------------------------------------------------------------------------------------


def test_mint_app_jwt_signs_claims_github_accepts(app_configured: Any) -> None:
    """RS256, issued by the App id, backdated `iat`, and an `exp` inside GitHub's ten minutes."""
    token = mint_app_jwt()

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"

    claims = jwt.decode(token, app_configured, algorithms=["RS256"])
    now = int(time.time())
    assert claims["iss"] == _APP_ID
    # Backdated, which is the whole point of `_JWT_BACKDATE_S`: a clock a few seconds fast would
    # otherwise produce an `iat` in GitHub's future and be rejected outright.
    assert claims["iat"] <= now
    assert claims["exp"] > now
    # Under GitHub's hard ceiling, asserted against the SUM of lifetime and backdate rather than
    # against either constant alone. That is the invariant: `iat` is backdated, so the window
    # GitHub measures is `_JWT_LIFETIME_S + _JWT_BACKDATE_S`, and this assertion is what catches
    # either number being raised on its own into a JWT GitHub rejects.
    assert claims["exp"] - claims["iat"] < github_app._JWT_MAX_WINDOW_S


def test_mint_app_jwt_accepts_a_key_with_escaped_newlines(
    monkeypatch: pytest.MonkeyPatch, rsa_pem: tuple[str, Any]
) -> None:
    """A PEM that arrived through a shell as `\\n`-escaped text still signs.

    This is what `fly secrets set` frequently produces, and a PEM with no real line breaks is not a
    PEM — `cryptography` rejects it with a message that says nothing about newlines.
    """
    pem, public_key = rsa_pem
    monkeypatch.setattr(settings, "github_app_id", _APP_ID)
    monkeypatch.setattr(settings, "github_app_private_key", pem.replace("\n", "\\n"))

    claims = jwt.decode(mint_app_jwt(), public_key, algorithms=["RS256"])
    assert claims["iss"] == _APP_ID


def test_mint_app_jwt_without_a_key_raises_and_says_which_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_app_private_key", "")
    monkeypatch.setattr(settings, "github_app_id", _APP_ID)

    with pytest.raises(GitHubAuthError) as caught:
        mint_app_jwt()
    assert "GITHUB_APP_PRIVATE_KEY" in str(caught.value)


def test_a_malformed_key_does_not_appear_in_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure names the variable, never quotes the value.

    A key-parsing error from `cryptography` can echo its input, so the exception's own text is
    deliberately not included in the message this module raises.
    """
    monkeypatch.setattr(settings, "github_app_id", _APP_ID)
    monkeypatch.setattr(settings, "github_app_private_key", "-----BEGIN PRIVATE KEY-----\nnope\n")

    with pytest.raises(GitHubAuthError) as caught:
        mint_app_jwt()
    message = str(caught.value)
    assert "nope" not in message
    assert "GITHUB_APP_PRIVATE_KEY" in message


def test_installation_token_repr_is_redacted() -> None:
    """A dataclass prints every field by default, and this one holds a live write credential."""
    token = InstallationToken(token="ghs_supersecret", expires_at=123.0)

    assert "ghs_supersecret" not in repr(token)
    assert "***" in repr(token)


# ---------------------------------------------------------------------------------------
# The installation token exchange
# ---------------------------------------------------------------------------------------


async def test_installation_token_exchanges_and_caches(app_configured: Any) -> None:
    """One exchange, then the cache — and the App JWT is what authorizes the exchange."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"token": "ghs_abc", "expires_at": "2026-01-01T00:00:00Z"})

    async with _client(handler) as client:
        first = await installation_token(client, 42)
        second = await installation_token(client, 42)

    assert first.token == "ghs_abc"
    assert second.token == "ghs_abc"
    assert len(calls) == 1, "the second call must be served from the cache"

    # The exchange presents the APP JWT, not an installation token — the distinction the module
    # docstring calls the most likely mistake in its subject matter.
    presented = calls[0].headers["Authorization"].removeprefix("Bearer ")
    assert jwt.decode(presented, app_configured, algorithms=["RS256"])["iss"] == _APP_ID


async def test_forget_installation_token_forces_a_fresh_exchange(app_configured: Any) -> None:
    """What a `401` on a cached token has to be able to do."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"token": f"ghs_{len(calls)}"})

    async with _client(handler) as client:
        await installation_token(client, 42)
        forget_installation_token(42)
        second = await installation_token(client, 42)

    assert len(calls) == 2
    assert second.token == "ghs_2"


async def test_a_404_exchange_mentions_uninstallation(app_configured: Any) -> None:
    """`404` is what an uninstalled App looks like, and the message says so.

    Worth its own test because it is the one failure here that is a legitimate end state rather
    than an outage, and the copy is what tells a user to reconnect instead of wait.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        with pytest.raises(GitHubAuthError) as caught:
            await installation_token(client, 42)
    assert "uninstalled" in str(caught.value)


async def test_a_token_response_with_no_token_raises(app_configured: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"expires_at": "2026-01-01T00:00:00Z"})

    async with _client(handler) as client:
        with pytest.raises(GitHubAuthError):
            await installation_token(client, 42)


async def test_fetch_installation_account_verifies_before_trusting(app_configured: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/42"
        return httpx.Response(200, json={"account": {"login": "acme-org"}})

    async with _client(handler) as client:
        assert await fetch_installation_account(client, 42) == "acme-org"


async def test_fetch_installation_account_rejects_an_unknown_id(app_configured: Any) -> None:
    """The check that keeps a number typed into a URL bar out of `github_installations`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        with pytest.raises(GitHubAuthError):
            await fetch_installation_account(client, 999)


# ---------------------------------------------------------------------------------------
# The repository calls
# ---------------------------------------------------------------------------------------


async def test_read_file_returns_absence_for_a_missing_path() -> None:
    """A first publication's normal state, and not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        found = await read_file(client, "tok", owner="o", repo="r", path="llms.txt", ref="main")
    assert found.exists is False
    assert found.text is None


async def test_read_file_decodes_base64_and_carries_the_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # "# Acme\n" base64-encoded.
        return httpx.Response(
            200, json={"encoding": "base64", "content": "IyBBY21lCg==", "sha": "blob1"}
        )

    async with _client(handler) as client:
        found = await read_file(client, "tok", owner="o", repo="r", path="llms.txt", ref="main")
    assert found.text == "# Acme\n"
    assert found.sha == "blob1"
    assert found.exists is True


async def test_read_file_reports_a_directory_as_a_usable_error() -> None:
    """The Contents API answers a directory with a JSON array, and the message says what to do."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "a"}])

    async with _client(handler) as client:
        with pytest.raises(GitHubApiError) as caught:
            await read_file(client, "tok", owner="o", repo="r", path="docs", ref="main")
    assert "directory" in str(caught.value)


async def test_read_file_treats_an_oversized_file_as_unknown_not_absent() -> None:
    """A file over 1 MB comes back with `encoding: "none"`: no text, but a real sha.

    The distinction matters — `text=None` with a sha means "cannot compare, overwrite it", while
    `text=None` with no sha means "create it". Collapsing the two would make a publication either
    skip a change or send the wrong sha.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"encoding": "none", "content": "", "sha": "big1"})

    async with _client(handler) as client:
        found = await read_file(client, "tok", owner="o", repo="r", path="big.txt", ref="main")
    assert found.text is None
    assert found.exists is True


async def test_the_token_is_sent_as_a_header_and_never_in_the_url() -> None:
    """A credential in a query string lands in GitHub's access logs and in every redirect."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(handler) as client:
        await read_file(client, "ghs_secret", owner="o", repo="r", path="llms.txt", ref="main")

    assert seen[0].headers["Authorization"] == "Bearer ghs_secret"
    assert "ghs_secret" not in str(seen[0].url)


async def test_write_file_sends_the_sha_when_overwriting() -> None:
    """The conditional write that makes a publication safe against a concurrent hand edit."""
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"commit": {"sha": "commit1"}})

    async with _client(handler) as client:
        sha = await write_file(
            client,
            "tok",
            owner="o",
            repo="r",
            path="llms.txt",
            branch="b",
            text="# Acme\n",
            message="Update llms.txt",
            sha="blob1",
        )

    assert sha == "commit1"
    assert bodies[0]["sha"] == "blob1"
    assert bodies[0]["branch"] == "b"


async def test_write_file_omits_the_sha_when_creating() -> None:
    """Sending a sha for a file that does not exist is a `422`, so it must be absent."""
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"commit": {"sha": "commit1"}})

    async with _client(handler) as client:
        await write_file(
            client,
            "tok",
            owner="o",
            repo="r",
            path="llms.txt",
            branch="b",
            text="# Acme\n",
            message="Create llms.txt",
            sha=None,
        )

    assert "sha" not in bodies[0]


async def test_write_file_explains_a_conflict() -> None:
    """`409` means the file changed between our read and our write, and the message says that."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "is at abc but expected def"})

    async with _client(handler) as client:
        with pytest.raises(GitHubApiError) as caught:
            await write_file(
                client,
                "tok",
                owner="o",
                repo="r",
                path="llms.txt",
                branch="b",
                text="x",
                message="m",
                sha="stale",
            )
    assert "changed in the repository" in str(caught.value)


async def test_create_branch_treats_an_existing_ref_as_success() -> None:
    """A retried worker job must converge on the branch its first attempt made."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Reference already exists"})

    async with _client(handler) as client:
        await create_branch(
            client, "tok", owner="o", repo="r", branch="b", from_sha="sha1"
        )  # must not raise


async def test_create_branch_raises_on_a_real_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    async with _client(handler) as client:
        with pytest.raises(GitHubApiError):
            await create_branch(client, "tok", owner="o", repo="r", branch="b", from_sha="sha1")


async def test_branch_head_sha_reads_the_ref() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/o/r/git/ref/heads/main"
        return httpx.Response(200, json={"object": {"sha": "head1"}})

    async with _client(handler) as client:
        assert await branch_head_sha(client, "tok", owner="o", repo="r", branch="main") == "head1"


async def test_create_pull_request_returns_number_and_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/o/r/pull/7"})

    async with _client(handler) as client:
        number, url = await create_pull_request(
            client, "tok", owner="o", repo="r", head="h", base="main", title="t", body="b"
        )
    assert (number, url) == (7, "https://github.com/o/r/pull/7")


async def test_list_repositories_reports_truncation() -> None:
    """A prefix must not be presented as the whole list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_count": 250,
                "repositories": [
                    {
                        "name": "site",
                        "owner": {"login": "acme"},
                        "default_branch": "trunk",
                        "private": True,
                    }
                ],
            },
        )

    async with _client(handler) as client:
        repositories, truncated = await list_repositories(client, "tok")

    assert truncated is True
    assert repositories[0].owner == "acme"
    assert repositories[0].default_branch == "trunk"
    assert repositories[0].private is True


async def test_a_transport_failure_becomes_a_github_api_error() -> None:
    """No `httpx` exception escapes the client — callers catch one type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as client:
        with pytest.raises(GitHubApiError):
            await read_file(client, "tok", owner="o", repo="r", path="llms.txt", ref="main")
