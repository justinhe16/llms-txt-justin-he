"""Tests for `GET /websites/{id}/stats`: the pre-aggregated run-statistics endpoint.

Every test here drives the real application (`app.main.app`) over a real Postgres, with a
real signed JWT — see `tests/conftest.py`'s "signed-in API clients" section, the same rig
`tests/test_runs_api.py` uses. As with that suite, the claims worth testing are exactly the
ones a stub would paper over:

* **Any signed-in user reads any website's stats.** `test_a_non_owner_can_read_the_stats` is
  the test that fails the day someone adds a well-meaning `WHERE user_id = $1` to
  `RunsReader.website_stats` (ARCHITECTURE.md §4.1).
* **One aggregate query, zero-filled, whose two averages count different populations.**
  `test_seeded_runs_produce_hand_verifiable_aggregates` and the population tests below
  hand-compute every number the endpoint returns rather than merely asserting "some number
  came back" — `internals/runs_reader.py`'s `_WEBSITE_STATS` is exactly the kind of query
  that can look right while quietly double-counting or under-counting a bucket at its edge,
  or averaging `pages_crawled` and `duration_ms` over the same set of rows when they must
  not be.

Every test seeds its own website with a unique origin and reads only that website's stats
(`GET /websites/{id}/stats` is scoped by `website_id` in its own right — the endpoint's
documented contract, not an ownership check, see ARCHITECTURE.md §4.1), so equality
assertions on one website's stats are safe even though `TEST_DATABASE_URL` may be a
developer's local database holding unrelated rows.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from asyncpg import Pool
from conftest import TEST_USER_A_ID, explain_plan, parse, plan_nodes, seed_run, seed_website
from httpx import AsyncClient

from app.features.runs.internals.runs_reader import _LATEST_COMPLETED_INDEX, _WEBSITE_STATS
from app.features.runs.internals.stats_window import resolve_window


# A time base for seeded runs, fixed relative to "now". NOT shared with any other suite's
# `_NOW` — see `conftest.py`'s seed-helper section docstring for why each suite keeps its own.
_NOW = datetime.now(UTC)


# -----------------------------------------------------------------------------------------
# PER-199: a frozen clock for the two tests below that assert on `window.start`/`window.end`
# THEMSELVES, rather than merely somewhere comfortably inside the window.
#
# Every other test in this file that calls `resolve_window("3d", _NOW)` only uses the result
# to find a bucket well inside the window (`window.start + timedelta(hours=36)`), and stays
# correct even if the server's own `datetime.now(UTC)` call (`RunService.get_website_stats`,
# `service.py:1016`) resolves a window shifted by the one bucket a single hour boundary
# crossed between module-import time and request time can produce — the shifted bucket is
# still inside the new window either way, with 35 buckets of slack on either side. The two
# tests below instead seed AT `window.start` and `window.end` exactly, which is exactly what
# a one-bucket shift moves out from under them: one of three seeded buckets stops being
# counted (`assert 2 == 3`), or a run seeded exactly at
# `window.start` lands one bucket outside the server's own, differently-shifted window
# (`assert 0 == 1`). That was reproduced and reverted while fixing this — see the PR
# description for the reproduction.
#
# `frozen_stats_clock` removes the race rather than narrowing it: it monkeypatches
# `app.features.runs.service.datetime` (that module does `from datetime import UTC, datetime,
# timedelta`, so `app.features.runs.service.datetime` is the correct dotted path — the same
# idiom `test_run_persistence.py` uses for `app.features.runs.service.transaction`) with a
# subclass whose `.now(tz)` always returns one fixed instant, and hands that SAME instant back
# so a test can resolve its own expected window from it. The frozen instant is deliberately a
# few seconds after UTC midnight — the exact boundary this bug crossed — so these two tests
# exercise it on every single run, not just the one run in twenty that used to catch it live.
# -----------------------------------------------------------------------------------------


def _frozen_datetime(frozen: datetime) -> type[datetime]:
    """Build a `datetime` subclass whose `.now(tz)` always returns `frozen`.

    A fresh class per call, never a module-level singleton, so two tests freezing two
    different instants in the same session can never share — or clobber — one class
    attribute.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return frozen.astimezone(tz) if tz is not None else frozen

    return _Frozen


@pytest.fixture
def frozen_stats_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the instant `RunService.get_website_stats` resolves its `?window=` against, and
    return that same instant so a test can resolve an identical window itself — see the
    section docstring above for the race this closes and why the instant is chosen where it
    is.
    """
    frozen = datetime(2026, 3, 15, 0, 0, 7, tzinfo=UTC)
    monkeypatch.setattr("app.features.runs.service.datetime", _frozen_datetime(frozen))
    return frozen


def _series_bucket(series: list[dict[str, Any]], t: datetime) -> dict[str, Any]:
    """Find the one `series` entry whose bucket start is exactly `t`.

    Fails with every bucket start actually present rather than a bare `IndexError` or
    `KeyError` — more useful when a test's own boundary arithmetic is the thing that is off
    by one bucket, not the endpoint.
    """
    matches = [point for point in series if parse(point["t"]) == t]
    assert len(matches) == 1, (t, [point["t"] for point in series])
    return matches[0]


def _assert_active_buckets_are_exactly(
    series: list[dict[str, Any]], expected: set[datetime]
) -> None:
    """Assert the buckets with `runs > 0` are exactly `expected`.

    Names which bucket timestamps are missing or unexpected on failure (PER-199), rather than
    two bare integers like `assert 2 == 3` — a mismatch here almost always means a window
    boundary moved between when the test seeded its rows and when the request resolved its
    own window, and naming the timestamps is what makes that readable straight from the
    assertion rather than from a debugger session.
    """
    actual = {parse(point["t"]) for point in series if point["runs"] > 0}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    assert not missing and not unexpected, (
        f"active buckets do not match -- missing {missing}, unexpected {unexpected} "
        f"(expected {sorted(expected)}, got {sorted(actual)}); a window boundary likely "
        "moved between seeding and the request"
    )


def _compared_diff(
    *,
    pages_added: int = 0,
    pages_removed: int = 0,
    metadata_changed: int | None = 0,
    content_changed: int | None = None,
    metadata_not_comparable_reason: str | None = None,
    compared_to_run_id: str = "11111111-1111-1111-1111-111111111111",
) -> dict[str, Any]:
    """A `"compared"`-shaped `index_diff` block, valid enough to pass `RunIndexDiff.
    model_validate` — the PER-193/PER-194 counterpart to this file's other seeded-`stats`
    helpers, using the current wire field names (`metadata_changed`/`content_changed`) rather
    than the pre-PER-194 `pages_changed`/`changed_sample` — see `test_stats_api.py`'s own
    version-7-compat test for the ONE place this suite deliberately builds the old shape."""
    return {
        "state": "compared",
        "previous_run_completed": True,
        "compared_to_run_id": compared_to_run_id,
        "compared_to_completed_at": "2026-01-01T00:00:00+00:00",
        "pages_added": pages_added,
        "pages_removed": pages_removed,
        "metadata_changed": metadata_changed,
        "added_sample": [],
        "removed_sample": [],
        "metadata_changed_sample": [],
        "metadata_not_comparable_reason": metadata_not_comparable_reason,
        "content_changed": content_changed,
        "content_changed_sample": [],
        "urls_discovered_delta": None,
        "sections_delta": {},
        "selection_churn": pages_added + pages_removed,
        "selection_churn_ratio": None,
        "llms_txt_bytes_delta": 0,
    }


def _stats_with_diff(
    *,
    pages_crawled: int = 1,
    links_emitted: int = 1,
    links_optional: int | None = None,
    llms_txt_bytes: int = 100,
    index_diff: Any = None,
) -> dict[str, Any]:
    """A `RUN_STATS_VERSION` 8-shaped `stats` dict, carrying only the keys the queries and
    `_to_latest` under test actually read.

    `links_optional` defaults to `None` — ABSENT from the returned dict, never a `0` key — so
    every test that does not pass it stays a genuine pre-`RUN_STATS_VERSION`-13 fixture,
    exercising the same "no `links_optional` key at all" path a real pre-13 row is in. A test
    that wants to exercise the version-13 `links_emitted + links_optional` sum passes an
    explicit `int`."""
    stats: dict[str, Any] = {
        "pages_crawled": pages_crawled,
        "links_emitted": links_emitted,
        "llms_txt_bytes": llms_txt_bytes,
        "index_diff": index_diff,
        "version": 8,
    }
    if links_optional is not None:
        stats["links_optional"] = links_optional
        stats["version"] = 13
    return stats


# -----------------------------------------------------------------------------------------
# 1. Hand-verifiable aggregates
# -----------------------------------------------------------------------------------------


async def test_seeded_runs_produce_hand_verifiable_aggregates(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-hand-verified.example"
    )
    window = resolve_window("3d", _NOW)
    # A bucket boundary well inside the window -- not `window.start` itself, so this test
    # exercises an ordinary bucket rather than doubling as the boundary test (see #13 below).
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, milliseconds=10_000),
        status="completed",
        stats={"pages_crawled": 10},
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=20),
        completed_at=target_bucket + timedelta(minutes=20, milliseconds=20_000),
        status="completed",
        stats={"pages_crawled": 20},
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=30),
        completed_at=target_bucket + timedelta(minutes=30, milliseconds=30_000),
        status="completed",
        stats={"pages_crawled": 30},
    )
    # A failed run: contributes to `runs`/`failed` and to the `avg_pages` denominator (as 0
    # pages, no stats recorded), but is excluded from the duration average entirely.
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=40),
        status="failed",
        completed_at=None,
        stats=None,
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "3d"
    assert body["bucket"] == "hour"

    bucket = _series_bucket(body["series"], target_bucket)
    assert bucket["runs"] == 4
    assert bucket["completed"] == 3
    assert bucket["failed"] == 1
    # avg_pages over ALL 4 runs (the failed one contributes 0): (10+20+30+0) / 4 = 15.0
    assert bucket["avg_pages"] == pytest.approx(15.0)
    # avg_duration_ms over only the 3 COMPLETED runs: (10000+20000+30000) / 3 = 20000
    assert bucket["avg_duration_ms"] == 20_000

    totals = body["totals"]
    assert totals["total_runs"] == 4
    assert totals["completed"] == 3
    assert totals["failed"] == 1
    assert totals["success_rate"] == pytest.approx(0.75)
    assert totals["avg_duration_ms"] == 20_000
    assert totals["avg_pages"] == pytest.approx(15.0)
    assert parse(totals["last_run_at"]) == target_bucket + timedelta(minutes=40)


# -----------------------------------------------------------------------------------------
# 2. Zero-fill over a sparse history
# -----------------------------------------------------------------------------------------


async def test_a_3_day_window_with_runs_in_three_buckets_zero_fills_the_rest(
    user_client: AsyncClient, websites_db: Pool, frozen_stats_clock: datetime
) -> None:
    """PER-199: uses `frozen_stats_clock` rather than the module-level `_NOW`, because this
    test seeds AT `window.start`/`window.end` themselves — see that fixture's section
    docstring for the flake this closes."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-sparse.example")
    window = resolve_window("3d", frozen_stats_clock)
    active_buckets = [window.start, window.start + timedelta(hours=36), window.end - window.step]

    for bucket_start in active_buckets:
        await seed_run(websites_db, website_id, started_at=bucket_start + timedelta(minutes=6))

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    series = response.json()["series"]
    assert len(series) == 72

    _assert_active_buckets_are_exactly(series, set(active_buckets))

    for point in series:
        if point["runs"] == 0:
            assert point["completed"] == 0
            assert point["failed"] == 0
            assert point["avg_pages"] == 0
            assert point["avg_duration_ms"] == 0


# -----------------------------------------------------------------------------------------
# 3. No runs at all
# -----------------------------------------------------------------------------------------


async def test_a_website_with_no_runs_returns_zeroed_buckets_and_null_summary(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-empty.example")

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    body = response.json()
    # The default window is 1d/hour — 24 zeroed buckets.
    assert len(body["series"]) == 24
    assert all(point["runs"] == 0 for point in body["series"])

    totals = body["totals"]
    assert totals["total_runs"] == 0
    assert totals["completed"] == 0
    assert totals["failed"] == 0
    assert totals["avg_duration_ms"] == 0
    assert totals["avg_pages"] == 0
    # Explicitly `is None`, not merely falsy -- a `0.0` success_rate would misreport "no
    # runs yet" as "100% failure".
    assert totals["success_rate"] is None
    assert totals["last_run_at"] is None


# -----------------------------------------------------------------------------------------
# 4. Failed runs: excluded from the duration average, counted everywhere else
# -----------------------------------------------------------------------------------------


async def test_failed_runs_are_excluded_from_the_duration_average_but_counted(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-failed.example")
    completed_started = _NOW - timedelta(hours=2)
    await seed_run(
        websites_db,
        website_id,
        started_at=completed_started,
        completed_at=completed_started + timedelta(milliseconds=5_000),
        status="completed",
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=1),
        status="failed",
        completed_at=None,
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["total_runs"] == 2
    assert totals["failed"] == 1
    assert totals["completed"] == 1
    # The failed run has no duration to contribute -- the average is over the one
    # completed run alone, not dragged toward zero by a non-existent duration.
    assert totals["avg_duration_ms"] == 5_000


# -----------------------------------------------------------------------------------------
# 5. Malformed / absent `stats` never 500s, and count as zero pages
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stats",
    [
        pytest.param(None, id="no-stats-at-all"),
        pytest.param({"tokens": 4096}, id="stats-present-without-pages-crawled"),
        pytest.param('{"pages_crawled": "not-a-number"}', id="non-numeric-pages-crawled"),
    ],
)
async def test_malformed_or_absent_stats_count_as_zero_pages_and_do_not_500(
    user_client: AsyncClient, websites_db: Pool, stats: dict[str, Any] | str | None
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-malformed.example")
    await seed_run(websites_db, website_id, started_at=_NOW, stats=stats)

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["total_runs"] == 1
    assert totals["avg_pages"] == 0


# -----------------------------------------------------------------------------------------
# 6. In-flight runs: excluded from the duration average, counted everywhere else
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "processing"])
async def test_in_flight_runs_are_excluded_from_the_duration_average_but_counted(
    user_client: AsyncClient, websites_db: Pool, status: str
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://stats-inflight-{status}.example"
    )
    completed_started = _NOW - timedelta(hours=3)
    await seed_run(
        websites_db,
        website_id,
        started_at=completed_started,
        completed_at=completed_started + timedelta(milliseconds=8_000),
        status="completed",
    )
    await seed_run(websites_db, website_id, started_at=_NOW, status=status, completed_at=None)

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["total_runs"] == 2
    assert totals["avg_duration_ms"] == 8_000


# -----------------------------------------------------------------------------------------
# 7. Invalid `window` -> 422
# -----------------------------------------------------------------------------------------


# `7d` and `14d` are in this list on purpose: they were valid windows before this set became
# 12h/1d/3d, so a bookmarked link still carries them. The backend rejects them outright; the
# frontend never sends one, because `isStatsWindow` (lib/crawls/use-detail-view.ts) falls back
# to the default before the request is ever built.
@pytest.mark.parametrize("window", ["7d", "14d", "30d", "90d", "30", "", "DROP TABLE"])
async def test_an_invalid_window_value_is_422(
    user_client: AsyncClient, websites_db: Pool, window: str
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-bad-window.example")

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": window})

    assert response.status_code == 422


# -----------------------------------------------------------------------------------------
# 8. & 9. window/bucket combinations, including the default
# -----------------------------------------------------------------------------------------


async def test_default_window_is_1d_and_bucket_is_hour(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-default-window.example"
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1d"
    assert body["bucket"] == "hour"
    assert len(body["series"]) == 24


async def test_12h_returns_12_hourly_buckets(user_client: AsyncClient, websites_db: Pool) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-12h.example")

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "12h"})

    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "12h"
    assert body["bucket"] == "hour"
    assert len(body["series"]) == 12


async def test_1d_returns_24_hourly_buckets(user_client: AsyncClient, websites_db: Pool) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-1d.example")

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "1d"})

    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1d"
    assert body["bucket"] == "hour"
    assert len(body["series"]) == 24


async def test_3d_returns_72_hourly_buckets(user_client: AsyncClient, websites_db: Pool) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-3d.example")

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "3d"
    assert body["bucket"] == "hour"
    assert len(body["series"]) == 72


# -----------------------------------------------------------------------------------------
# 10-12. Authorization and 404s
# -----------------------------------------------------------------------------------------


async def test_a_non_owner_can_read_the_stats(
    second_user_client: AsyncClient, websites_db: Pool
) -> None:
    """Reads are unscoped (ARCHITECTURE.md §4.1). The website below is owned by
    `TEST_USER_A_ID`; `second_user_client` is signed in as `TEST_USER_B_ID` and must still
    get a `200` with the real aggregate. This is the test that fails the day
    `RunsReader.website_stats` grows a `WHERE user_id = $1`.
    """
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-not-mine.example")
    await seed_run(websites_db, website_id, started_at=_NOW)

    response = await second_user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    assert response.json()["totals"]["total_runs"] == 1


async def test_unauthenticated_is_401(unauthenticated_client: AsyncClient) -> None:
    response = await unauthenticated_client.get(f"/websites/{uuid4()}/stats")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_unknown_website_is_404(user_client: AsyncClient) -> None:
    response = await user_client.get(f"/websites/{uuid4()}/stats")

    assert response.status_code == 404


# -----------------------------------------------------------------------------------------
# 13. The bucket-edge boundary: [start, end) is half-open, not approximately so
# -----------------------------------------------------------------------------------------


async def test_a_run_exactly_at_window_start_is_counted_one_microsecond_earlier_is_not(
    user_client: AsyncClient, websites_db: Pool, frozen_stats_clock: datetime
) -> None:
    """PER-199: uses `frozen_stats_clock` rather than the module-level `_NOW`, because this
    test seeds AT `window.start` itself — see that fixture's section docstring for the flake
    this closes."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-boundary.example")
    window = resolve_window("3d", frozen_stats_clock)

    await seed_run(websites_db, website_id, started_at=window.start)
    await seed_run(websites_db, website_id, started_at=window.start - timedelta(microseconds=1))

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    body = response.json()
    # Only the in-window run is counted -- the one microsecond before it falls outside
    # [window.start, window.end) and must not appear anywhere in the response.
    assert body["totals"]["total_runs"] == 1

    first_bucket = _series_bucket(body["series"], window.start)
    assert first_bucket["runs"] == 1, (
        f"expected the run seeded exactly at window.start ({window.start.isoformat()}) to be "
        f"counted in that bucket, got {first_bucket['runs']} -- a window boundary likely "
        "moved between seeding and the request"
    )

    # Nothing is double-counted at the edge: the series and the totals must agree exactly.
    assert sum(point["runs"] for point in body["series"]) == body["totals"]["total_runs"]


# -----------------------------------------------------------------------------------------
# 14. Isolation between websites
# -----------------------------------------------------------------------------------------


async def test_runs_belonging_to_a_different_website_are_not_counted(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-mine.example")
    other_website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-other.example"
    )
    await seed_run(websites_db, website_id, started_at=_NOW)
    await seed_run(websites_db, other_website_id, started_at=_NOW)
    await seed_run(websites_db, other_website_id, started_at=_NOW - timedelta(hours=1))

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    assert response.json()["totals"]["total_runs"] == 1


# -----------------------------------------------------------------------------------------
# PER-193: the index diff, folded into `series`/`totals`, and `latest` — the newest
# completed run's own diff, window-scoped.
# -----------------------------------------------------------------------------------------


async def test_series_reports_pages_added_and_removed_from_the_diff_block(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-diff-series.example"
    )
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="completed",
        stats=_stats_with_diff(index_diff=_compared_diff(pages_added=3, pages_removed=1)),
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=20),
        completed_at=target_bucket + timedelta(minutes=20, seconds=1),
        status="completed",
        stats=_stats_with_diff(index_diff=_compared_diff(pages_added=2, pages_removed=4)),
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    body = response.json()
    bucket = _series_bucket(body["series"], target_bucket)
    assert bucket["runs_compared"] == 2
    assert bucket["pages_added"] == 5
    assert bucket["pages_removed"] == 5
    assert body["totals"]["runs_compared"] == 2
    assert body["totals"]["pages_added"] == 5
    assert body["totals"]["pages_removed"] == 5


async def test_index_size_is_the_last_completed_run_in_the_bucket_not_the_max(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A `max()` implementation would report the LARGER of the two — 5000 bytes / 50 pages —
    which is exactly wrong the moment a site's index shrinks between two runs in one bucket."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-index-size.example")
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="completed",
        stats=_stats_with_diff(links_emitted=50, llms_txt_bytes=5_000, index_diff=None),
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=20),
        completed_at=target_bucket + timedelta(minutes=20, seconds=1),
        status="completed",
        stats=_stats_with_diff(links_emitted=10, llms_txt_bytes=900, index_diff=None),
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["index_pages"] == 10
    assert bucket["index_bytes"] == 900


async def test_a_pre_version_13_row_reports_links_emitted_alone_as_index_pages(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A row with no `links_optional` key at all — every row before the curated-index ticket
    — reports `index_pages` as exactly `links_emitted`, its pre-13 meaning: that key adds
    nothing it never had, because `_WEBSITE_STATS`'s inner `links_optional` guard defaults to
    `0` only INSIDE the `links_emitted` sum, never as a separate contribution the outer
    `status = 'completed'` guard could turn into a false zero."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-pre-v13-index.example"
    )
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="completed",
        stats=_stats_with_diff(links_emitted=12, llms_txt_bytes=1_200, index_diff=None),
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["index_pages"] == 12


async def test_a_version_13_row_sums_main_and_optional_links_as_index_pages(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The curated-index ticket's own redefinition: once `links_optional` is recorded,
    `index_pages` is the SUM — the tile keeps its "pages in the artifact" meaning even though
    some of them now render under `## Optional` rather than the main body."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-v13-index.example")
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="completed",
        stats=_stats_with_diff(
            links_emitted=12, links_optional=5, llms_txt_bytes=1_200, index_diff=None
        ),
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["index_pages"] == 17


async def test_a_bucket_with_no_completed_run_reports_null_index_size_not_zero(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """The headline null-vs-zero assertion: a bucket with a run that never completed has no
    known index size, and that must render as `null`, never `0`."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-null-index.example")
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        status="failed",
        completed_at=None,
        stats=None,
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["index_pages"] is None
    assert bucket["index_bytes"] is None
    # The three PLAIN counts stay real zeroes -- nothing was measured, which IS the answer.
    assert bucket["runs_compared"] == 0
    assert bucket["pages_added"] == 0
    assert bucket["pages_removed"] == 0


async def test_a_run_without_a_diff_block_contributes_zero_added_and_is_not_counted_as_compared(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-v5-row.example")
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    version_5_stats = {"pages_crawled": 1, "links_emitted": 1, "version": 5}
    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="completed",
        stats=version_5_stats,
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["runs_compared"] == 0
    assert bucket["pages_added"] == 0
    assert bucket["pages_removed"] == 0
    # No `llms_txt_bytes`/`links_emitted`-as-index-size on a version-5 row either -- still
    # unknown, not zero.
    assert bucket["index_bytes"] is None


async def test_a_failed_run_carrying_a_diff_block_is_excluded_from_the_series(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A `failed` row CAN carry a fully-populated `index_diff` — `run_stats.py`'s own
    docstring explains why (the diff is computed before the failure that ends the attempt) —
    and the `status = 'completed'` filter is what keeps it out of every PER-193 aggregate."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-failed-diff.example"
    )
    window = resolve_window("3d", _NOW)
    target_bucket = window.start + timedelta(hours=36)

    await seed_run(
        websites_db,
        website_id,
        started_at=target_bucket + timedelta(minutes=10),
        completed_at=target_bucket + timedelta(minutes=10, seconds=1),
        status="failed",
        stats=_stats_with_diff(index_diff=_compared_diff(pages_added=100, pages_removed=100)),
    )

    response = await user_client.get(f"/websites/{website_id}/stats", params={"window": "3d"})

    assert response.status_code == 200
    bucket = _series_bucket(response.json()["series"], target_bucket)
    assert bucket["runs_compared"] == 0
    assert bucket["pages_added"] == 0
    assert bucket["pages_removed"] == 0
    assert bucket["index_pages"] is None
    assert bucket["index_bytes"] is None


async def test_latest_reports_the_newest_completed_run_in_the_window(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-latest.example")

    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=2),
        completed_at=_NOW - timedelta(hours=2),
        status="completed",
        stats=_stats_with_diff(index_diff=None),
    )
    newer_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=1),
        completed_at=_NOW - timedelta(hours=1),
        status="completed",
        stats=_stats_with_diff(links_emitted=7, llms_txt_bytes=777, index_diff=None),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["run_id"] == str(newer_id)
    assert latest["index_pages"] == 7
    assert latest["index_bytes"] == 777


async def test_latest_sums_main_and_optional_links_when_recorded(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """`LatestRunSnapshot.index_pages` — `runs/service.py`'s `_index_pages` helper — holds the
    identical version-13 redefinition `_WEBSITE_STATS`'s SQL does for the bucketed series:
    `links_emitted + links_optional` once the row records both."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-latest-v13.example")

    run_id = await seed_run(
        websites_db,
        website_id,
        started_at=_NOW - timedelta(hours=1),
        completed_at=_NOW - timedelta(hours=1),
        status="completed",
        stats=_stats_with_diff(
            links_emitted=7, links_optional=3, llms_txt_bytes=777, index_diff=None
        ),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["run_id"] == str(run_id)
    assert latest["index_pages"] == 10


async def test_latest_is_null_when_the_window_has_no_completed_run(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-latest-null.example"
    )
    await seed_run(websites_db, website_id, started_at=_NOW, status="failed", completed_at=None)

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    assert response.json()["latest"] is None


async def test_latest_diff_state_is_not_recorded_for_a_pre_feature_row(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-latest-v5.example")
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats={"pages_crawled": 1, "links_emitted": 1, "version": 5},
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "not_recorded"
    assert latest["diff"] is None


async def test_latest_diff_state_is_first_run_for_a_first_run_block(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-latest-first.example"
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats=_stats_with_diff(index_diff={"state": "first_run", "previous_run_completed": None}),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "first_run"
    assert latest["previous_run_completed"] is None
    assert latest["diff"] is None


async def test_latest_diff_state_is_compared_and_carries_the_samples(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-latest-compared.example"
    )
    diff = _compared_diff(pages_added=1, pages_removed=0)
    diff["added_sample"] = [{"url": "https://example.test/new", "title": "New Page"}]

    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats=_stats_with_diff(index_diff=diff),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "compared"
    assert latest["previous_run_completed"] is True
    assert latest["diff"]["pages_added"] == 1
    assert latest["diff"]["added_sample"] == [
        {"url": "https://example.test/new", "title": "New Page"}
    ]
    assert latest["diff"]["metadata_changed"] == 0
    assert latest["diff"]["metadata_not_comparable_reason"] is None


async def test_a_not_comparable_metadata_diff_reaches_the_response_as_null_with_a_reason(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """A mode-flip diff (PER-194) reaches the API with `metadata_changed: null` and a named
    reason, while every mode-independent field — `pages_added`, `content_changed` — still
    reports a real number."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-not-comparable.example"
    )
    diff = _compared_diff(
        pages_added=1,
        pages_removed=0,
        metadata_changed=None,
        metadata_not_comparable_reason="enrichment_enabled",
        content_changed=2,
    )

    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats=_stats_with_diff(index_diff=diff),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "compared"
    assert latest["diff"]["metadata_changed"] is None
    assert latest["diff"]["metadata_changed_sample"] == []
    assert latest["diff"]["metadata_not_comparable_reason"] == "enrichment_enabled"
    assert latest["diff"]["content_changed"] == 2
    assert latest["diff"]["pages_added"] == 1


async def test_a_version_seven_diff_block_still_validates_and_reports_null_content_changed(
    user_client: AsyncClient, websites_db: Pool
) -> None:
    """Pins the `AliasChoices` decision on `RunIndexDiff` (PER-194): a version-7 row — written
    before `pages_changed`/`changed_sample` were renamed to `metadata_changed`/
    `metadata_changed_sample` — must still validate and reach the API as `diff_state ==
    "compared"`, never silently degrade to `"not_recorded"`. `content_changed`, which a
    version-7 row never recorded at all, reports `null` rather than `0`."""
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-v7-diff.example")
    version_7_diff = {
        "state": "compared",
        "previous_run_completed": True,
        "compared_to_run_id": "11111111-1111-1111-1111-111111111111",
        "compared_to_completed_at": "2026-01-01T00:00:00+00:00",
        "pages_added": 2,
        "pages_removed": 1,
        "pages_changed": 3,
        "added_sample": [],
        "removed_sample": [],
        "changed_sample": [],
        "urls_discovered_delta": None,
        "sections_delta": {},
        "selection_churn": 3,
        "selection_churn_ratio": None,
        "llms_txt_bytes_delta": 0,
    }
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats={
            "pages_crawled": 1,
            "links_emitted": 1,
            "llms_txt_bytes": 100,
            "index_diff": version_7_diff,
            "version": 7,
        },
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "compared"
    assert latest["diff"]["metadata_changed"] == 3
    assert latest["diff"]["content_changed"] is None
    assert latest["diff"]["content_changed_sample"] == []
    assert latest["diff"]["metadata_not_comparable_reason"] is None


@pytest.mark.parametrize(
    ("index_diff", "case_id"),
    [
        pytest.param("nonsense", "not-an-object", id="not-an-object"),
        pytest.param({"state": "compared"}, "missing-fields", id="missing-required-fields"),
    ],
)
async def test_a_malformed_index_diff_does_not_500_and_reports_not_recorded(
    user_client: AsyncClient, websites_db: Pool, index_diff: Any, case_id: str
) -> None:
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, f"https://stats-malformed-diff-{case_id}.example"
    )
    await seed_run(
        websites_db,
        website_id,
        started_at=_NOW,
        completed_at=_NOW,
        status="completed",
        stats=_stats_with_diff(index_diff=index_diff),
    )

    response = await user_client.get(f"/websites/{website_id}/stats")

    assert response.status_code == 200
    latest = response.json()["latest"]
    assert latest is not None
    assert latest["diff_state"] == "not_recorded"
    assert latest["diff"] is None


# -----------------------------------------------------------------------------------------
# The EXPLAIN test — this ticket's explicit acceptance criterion for the aggregate query.
#
# Deliberately a LONG, SPARSE history (~20k rows spread over roughly a decade) rather than
# `test_runs_api.py`'s dense, recent one: that suite's keyset query pages through "this
# website's whole history" and wants most of a shallow scan to matter, so a dense recent
# fixture is the right shape for it. This query's WHERE is a 3-day date range, and the claim
# under test is that such a narrow, *selective* range rides the index rather than being
# scanned sequentially — which only reproduces if the 3-day window is actually a small slice
# of the table. A dense recent history would make Postgres correctly prefer a Seq Scan, and a
# test asserting an index scan against that fixture would just be asserting the cost model,
# not this query's behavior.
# -----------------------------------------------------------------------------------------

_EXPLAIN_ROW_COUNT = 20_000
# ~10 years of history spread evenly across _EXPLAIN_ROW_COUNT rows: 10 * 365 * 86_400
# seconds, divided by the row count, rounded to a whole number of seconds. A 3-day window
# against this fixture covers roughly 3 / 3650 ≈ 0.08% of the table.
_EXPLAIN_STEP_SECONDS = (10 * 365 * 86_400) // _EXPLAIN_ROW_COUNT


async def _seed_long_sparse_history_for_explain(pool: Pool, website_id: UUID) -> None:
    """Bulk-insert `_EXPLAIN_ROW_COUNT` rows in one round trip, evenly spread across roughly
    a decade ending now, so a 3-day window from `resolve_window` is a small, highly
    selective slice of the table. See the section docstring above for why this fixture is
    sparse rather than dense, unlike `test_runs_api.py`'s.
    """
    await pool.execute(
        """
        INSERT INTO runs (website_id, "trigger", status, started_at, completed_at)
        SELECT
            $1,
            'manual'::run_trigger,
            'completed'::run_status,
            now() - ((i * $3) || ' seconds')::interval,
            now() - ((i * $3) || ' seconds')::interval + interval '1 minute'
        FROM generate_series(0, $2 - 1) AS s(i)
        """,
        website_id,
        _EXPLAIN_ROW_COUNT,
        _EXPLAIN_STEP_SECONDS,
    )
    await pool.execute("ANALYZE runs")


def _assert_no_seq_scan_and_touches_runs_exactly_once(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The two claims PER-156 makes about this query's plan.

    Deliberately does NOT reuse `test_runs_api.py`'s `_assert_uses_an_index_not_a_seq_scan`:
    that helper only accepts a plain `Index Scan` / `Index Only Scan`, and the plan this
    aggregate produces against a large, sparse table is a `Bitmap Index Scan` feeding a
    `Bitmap Heap Scan` instead — a legitimate, and for a moderately selective range query the
    EXPECTED, way of using the same composite index, not a regression.

    The second assertion — exactly one node whose `Relation Name` is `runs` — is the
    mechanical proof that this endpoint runs one aggregate query, not one query per bucket:
    `scoped` is read once (`AS MATERIALIZED` in `_WEBSITE_STATS`) and feeds both the
    per-bucket and the whole-window aggregate, so the base table is visited exactly once no
    matter how many buckets the window has.
    """
    nodes = plan_nodes(plan)
    node_types = [node["Node Type"] for node in nodes]
    assert "Seq Scan" not in node_types, node_types
    assert any(
        node_type in ("Index Scan", "Index Only Scan", "Bitmap Index Scan")
        for node_type in node_types
    ), node_types

    runs_scans = [node for node in nodes if node.get("Relation Name") == "runs"]
    assert len(runs_scans) == 1, (
        "Expected the plan to touch the `runs` relation exactly once -- the mechanical proof "
        f"of one aggregate query, no per-bucket queries. Node types: {node_types}"
    )
    return nodes


async def test_stats_query_plan_avoids_a_seq_scan_and_touches_runs_exactly_once(
    websites_db: Pool,
) -> None:
    website_id = await seed_website(websites_db, TEST_USER_A_ID, "https://stats-explain.example")
    await _seed_long_sparse_history_for_explain(websites_db, website_id)
    window = resolve_window("3d", _NOW)

    plan = await explain_plan(
        websites_db,
        _WEBSITE_STATS,
        website_id,
        window.start,
        window.end,
        window.step,
        window.bucket,
    )

    _assert_no_seq_scan_and_touches_runs_exactly_once(plan)


async def test_latest_completed_index_query_plan_avoids_a_seq_scan(websites_db: Pool) -> None:
    """`_LATEST_COMPLETED_INDEX`'s own EXPLAIN test, mirroring the one above — a `LIMIT 1`
    keyset lookup against the same sparse fixture, over the same 3-day window, must not fall
    back to a sequential scan of the table."""
    website_id = await seed_website(
        websites_db, TEST_USER_A_ID, "https://stats-latest-explain.example"
    )
    await _seed_long_sparse_history_for_explain(websites_db, website_id)
    window = resolve_window("3d", _NOW)

    plan = await explain_plan(
        websites_db, _LATEST_COMPLETED_INDEX, website_id, window.start, window.end
    )

    node_types = [node["Node Type"] for node in plan_nodes(plan)]
    assert "Seq Scan" not in node_types, node_types
