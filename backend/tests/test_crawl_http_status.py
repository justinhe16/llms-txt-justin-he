"""Tests for the two `app.features.crawl.service` helpers that turn a `SeedHttpError` into
something a user and the queue can each act on — `_seed_http_message` and `_is_retryable`.

No database and no network: both are pure functions of an exception, so everything here is an
in-memory call and an assertion on its return value, the same category
`tests/test_crawl_blocked.py` is in for `classify_block`.

**These cover the gap `internals/blocked.py` deliberately leaves.** `classify_block` answers
one narrow question — is this a WAF or a CDN refusing this crawler? — and answers `None` for a
`404`, a `429`, and an ordinary `5xx`, because those are a site answering honestly rather than
denying access. `SeedHttpError` owns exactly that remainder. The end-to-end behaviour (a run
reaching `failed` with the message this module pins) is asserted in
`tests/test_run_persistence.py`, which needs a live database; this file is the part that can
be checked without one.
"""

import httpx
import pytest

from app.features.crawl.internals.crawler import (
    AccessBlockedError,
    RobotsDisallowedError,
    SeedHttpError,
)
from app.features.crawl.service import _is_retryable, _safe_error_message, _seed_http_message


# --- The retry split ----------------------------------------------------------------------
#
# `SeedHttpError` is the only entry on `_is_retryable`'s list whose answer depends on a VALUE
# rather than only on a type, which is why it gets an explicit branch instead of membership in
# `_RETRYABLE_EXCEPTIONS`. Matching on the type alone would have to pick one answer for both a
# `404` and a `503`, and either choice is wrong half the time.


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599, 429])
def test_a_server_error_or_rate_limit_seed_is_retryable(status: int) -> None:
    """A site that is briefly unwell, or asking this crawler to slow down, may well answer
    differently on the next attempt — which is the whole question `_is_retryable` asks."""
    assert _is_retryable(SeedHttpError(status, "https://example.test/")) is True


@pytest.mark.parametrize("status", [400, 402, 404, 405, 410, 418, 451])
def test_an_ordinary_client_error_seed_is_permanent(status: int) -> None:
    """A `404` will still be a `404` on the third attempt. Retrying it delays an honest
    `failed` row by five minutes to reach the identical outcome."""
    assert _is_retryable(SeedHttpError(status, "https://example.test/")) is False


def test_the_other_seed_errors_keep_their_type_only_classification() -> None:
    """The new branch reads `.status` off a `SeedHttpError` and must not disturb how every
    other seed error is classified: a robots `Disallow` and a detected block are both
    permanent, and a timeout is still retryable."""
    assert _is_retryable(RobotsDisallowedError("robots.txt disallows /")) is False
    assert _is_retryable(AccessBlockedError("challenge", "https://example.test/")) is False
    assert _is_retryable(httpx.TimeoutException("too slow")) is True


# --- The messages -------------------------------------------------------------------------
#
# Four branches rather than one templated sentence: the statuses ask the user for four
# different things, and "the site returned HTTP {status}" would make every one of them the
# user's problem to diagnose.


def test_a_404_tells_the_user_to_check_the_url() -> None:
    message = _seed_http_message(404)

    assert "not found" in message.lower()
    assert "404" in message
    assert "retried" not in message, "a 404 is permanent; promising a retry would be a lie"


def test_a_429_says_it_will_be_retried() -> None:
    message = _seed_http_message(429)

    assert "429" in message
    assert "retried" in message


@pytest.mark.parametrize("status", [500, 503])
def test_a_server_error_names_the_status_and_says_it_will_be_retried(status: int) -> None:
    message = _seed_http_message(status)

    assert str(status) in message
    assert "retried" in message


def test_an_unusual_status_still_gets_a_usable_sentence() -> None:
    """The fallback branch. `402` is not one of the three cases worth its own advice, but a
    user still has to be told something other than nothing."""
    message = _seed_http_message(402)

    assert "402" in message
    assert message.endswith(".")


def test_401_and_403_never_reach_this_function() -> None:
    """`classify_block` claims both unconditionally (`_DENIED_STATUSES`), so a seed answering
    either becomes an `AccessBlockedError` and gets `_BLOCKED_MESSAGES`' allowlisting advice
    instead. Pinned as a fact about the two modules' division of labour: if `_DENIED_STATUSES`
    ever narrows, this assertion is what notices that the advice a user gets changed.
    """
    denied = _safe_error_message(AccessBlockedError("denied", "https://example.test/"))

    assert "allowlist" in denied
    assert "401 or 403" in denied


def test_the_message_a_run_records_comes_from_the_status() -> None:
    """`_safe_error_message` is the function `runs.error` is actually written from, so the
    `SeedHttpError` branch is checked through it rather than only through the helper it
    delegates to."""
    assert _safe_error_message(SeedHttpError(404, "https://example.test/")) == _seed_http_message(
        404
    )


def test_the_recorded_message_never_leaks_the_exceptions_own_text() -> None:
    """`SeedHttpError.__str__` carries the URL and is written for a developer reading
    `fly logs`. Every other branch of `_safe_error_message` avoids `str(exc)` for this reason
    and this one is held to the same bar."""
    exc = SeedHttpError(404, "https://internal.example.test/secret-path")

    assert "secret-path" not in _safe_error_message(exc)
