"""Tests for app.features.runs.internals.stats_window.resolve_window.

Pure-function tests, no database and no HTTP — the same spirit as tests/test_next_run.py and
tests/test_url_normalize.py: this module is the one place "what buckets does `?window=` mean,
and what half-open range of time do they cover" is decided, and it deserves the same
exhaustive, no-tolerance treatment those two get. Every assertion below is exact — never
`pytest.approx` — which is the entire point of `resolve_window` taking `now` as a required
argument instead of calling `datetime.now()` itself.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.features.runs.internals.stats_window import resolve_window
from app.features.runs.schemas import StatsBucketName, StatsWindowName


# Deliberately not on a bucket boundary: nonzero minute/second/microsecond, and an hour that
# is neither 0 nor 23, so a test that forgot to truncate would fail loudly instead of passing
# by coincidence.
_NOW = datetime(2026, 8, 5, 14, 37, 22, 123456, tzinfo=UTC)


# -----------------------------------------------------------------------------------------
# The documented window -> bucket / step / count mapping
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window_name", "bucket", "step", "bucket_count"),
    [
        pytest.param("12h", "hour", timedelta(hours=1), 12, id="12h-hourly"),
        pytest.param("1d", "hour", timedelta(hours=1), 24, id="1d-hourly"),
        pytest.param("3d", "hour", timedelta(hours=1), 72, id="3d-hourly"),
    ],
)
def test_each_window_maps_to_its_documented_bucket_step_and_count(
    window_name: StatsWindowName, bucket: StatsBucketName, step: timedelta, bucket_count: int
) -> None:
    result = resolve_window(window_name, _NOW)

    assert result.name == window_name
    assert result.bucket == bucket
    assert result.step == step
    assert result.bucket_count == bucket_count


def test_the_three_windows_share_the_hour_bucket_but_not_the_count() -> None:
    """Pins that no window is a copy-paste of another with the count forgotten. All three
    resolve to the same `bucket`/`step` — which is exactly why the counts are the only thing
    distinguishing them, and exactly why a wrong one would otherwise go unnoticed."""
    half_day = resolve_window("12h", _NOW)
    one_day = resolve_window("1d", _NOW)
    three_days = resolve_window("3d", _NOW)

    assert half_day.bucket == one_day.bucket == three_days.bucket == "hour"
    assert half_day.step == one_day.step == three_days.step == timedelta(hours=1)
    assert (half_day.bucket_count, one_day.bucket_count, three_days.bucket_count) == (12, 24, 72)


# -----------------------------------------------------------------------------------------
# UTC truncation
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("window_name", ["12h", "1d", "3d"])
def test_start_and_end_are_utc_truncated_to_the_bucket(window_name: StatsWindowName) -> None:
    result = resolve_window(window_name, _NOW)

    for instant in (result.start, result.end):
        assert instant.tzinfo == UTC
        assert instant.second == 0
        assert instant.microsecond == 0
        assert instant.minute == 0
        # Every window buckets hourly today, so the hour is carried through rather than
        # zeroed — the `day` branch of `_truncate` has no window to reach it. Asserted
        # rather than skipped: this is what "truncated to the bucket" currently MEANS.
        assert result.bucket == "hour"


def test_a_non_utc_input_now_is_normalised_before_truncation() -> None:
    """`.astimezone(UTC)` happens before truncation, not after — a `now` handed in some
    other offset must resolve to exactly the same window a UTC-supplied `now` for the same
    instant would, not a window shifted by the offset.
    """
    non_utc_now = _NOW.astimezone(timezone(timedelta(hours=-7)))

    assert resolve_window("3d", non_utc_now) == resolve_window("3d", _NOW)


# -----------------------------------------------------------------------------------------
# The half-open range: exact width, inclusive start, exclusive end
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("window_name", ["12h", "1d", "3d"])
def test_end_minus_start_is_exactly_bucket_count_steps(window_name: StatsWindowName) -> None:
    result = resolve_window(window_name, _NOW)

    assert result.end - result.start == result.bucket_count * result.step


def test_start_is_inclusive_and_end_is_exclusive_of_the_current_bucket() -> None:
    """The range must include the bucket `now` itself falls in — a run that started a moment
    ago must be counted — and `end` must be exactly one step past that bucket's start, never
    a whole extra bucket further out.
    """
    result = resolve_window("3d", _NOW)
    current_bucket_start = _NOW.replace(minute=0, second=0, microsecond=0)

    assert result.start <= current_bucket_start < result.end
    assert result.end == current_bucket_start + timedelta(hours=1)


@pytest.mark.parametrize("window_name", ["12h", "1d", "3d"])
def test_buckets_tile_the_window_with_no_gap_and_no_overlap(window_name: StatsWindowName) -> None:
    """Walking `step` from `start` exactly `bucket_count` times lands precisely on `end` —
    the property that makes `date_trunc`'s partition of `[start, end)` (in `runs_reader.py`'s
    `_WEBSITE_STATS`) gapless and non-overlapping, with no double-counted or dropped run at a
    bucket edge.
    """
    result = resolve_window(window_name, _NOW)

    cursor = result.start
    for _ in range(result.bucket_count):
        cursor += result.step

    assert cursor == result.end


def test_a_different_now_within_the_same_bucket_resolves_to_the_same_window() -> None:
    """Two instants in the same hour must produce byte-identical windows — the window is a
    function of which BUCKET `now` falls in, not of `now` to the microsecond.
    """
    later_in_the_same_hour = _NOW.replace(minute=58, second=59, microsecond=999999)

    assert resolve_window("3d", later_in_the_same_hour) == resolve_window("3d", _NOW)
