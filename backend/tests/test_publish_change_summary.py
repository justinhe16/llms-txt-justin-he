"""Tests for `app.features.publish.internals.change_summary` — deciding from a run's own diff
whether to publish, and what the commit message says.

Pure, no I/O: both functions take a `dict` and return a value.

**The property under test is the asymmetry**, not the formatting. A false "changed" costs a
`skipped_unchanged` row, because the repository's own copy is compared before anything is written.
A false "unchanged" silently stops publishing and the user never finds out. So every unreadable,
missing or unrecognized shape must resolve to `True`, and the tests below assert that for each
malformed shape individually rather than trusting one representative case.
"""

from typing import Any

from app.features.publish.internals.change_summary import (
    MAX_SUMMARY_CHARS,
    change_summary,
    index_changed,
)


def _compared(**overrides: Any) -> dict[str, Any]:
    """A `stats` dict whose `index_diff` compared two runs and found nothing changed."""
    diff = {
        "state": "compared",
        "pages_added": 0,
        "pages_removed": 0,
        "metadata_changed": False,
        "content_changed": False,
    }
    diff.update(overrides)
    return {"index_diff": diff}


class TestIndexChanged:
    def test_an_explicit_no_change_is_the_only_false(self) -> None:
        assert index_changed(_compared()) is False

    def test_pages_added_is_a_change(self) -> None:
        assert index_changed(_compared(pages_added=3)) is True

    def test_pages_removed_is_a_change(self) -> None:
        assert index_changed(_compared(pages_removed=1)) is True

    def test_metadata_changed_is_a_change(self) -> None:
        assert index_changed(_compared(metadata_changed=True)) is True

    def test_content_changed_is_a_change(self) -> None:
        assert index_changed(_compared(content_changed=True)) is True

    def test_a_first_run_publishes(self) -> None:
        """No previous index to compare against, so there is nothing to call unchanged."""
        assert index_changed({"index_diff": {"state": "first_run"}}) is True

    def test_every_unreadable_shape_publishes(self) -> None:
        """The publish-when-in-doubt bias, asserted shape by shape.

        Each of these is a real possibility: `stats` is jsonb, `index_diff` degrades to `None` when
        the diff read fails, and a row written by an older release carries whatever that release's
        `RUN_STATS_VERSION` defined. None of them is evidence that the index is unchanged.
        """
        unreadable: list[dict[str, Any] | None] = [
            None,
            {},
            {"index_diff": None},
            {"index_diff": "nope"},
            {"index_diff": []},
            {"index_diff": {}},
            {"index_diff": {"state": "not_comparable"}},
            {"index_diff": {"pages_added": 0}},  # no `state` at all
        ]
        for stats in unreadable:
            assert index_changed(stats) is True, stats

    def test_a_boolean_is_not_a_count(self) -> None:
        """`True` is an `int` in Python, and `pages_added: true` must not read as a count of 1.

        Not a hypothetical: jsonb holds either, `_count` is the only guard, and treating `True` as
        a page count would put "1 page added" into a commit message describing nothing.
        """
        assert index_changed(_compared(pages_added=True)) is False

    def test_a_negative_count_is_not_a_change(self) -> None:
        assert index_changed(_compared(pages_added=-2)) is False


class TestChangeSummary:
    def test_a_first_run_says_so(self) -> None:
        assert change_summary({"index_diff": {"state": "first_run"}}) == (
            "First generated index for this site."
        )

    def test_one_signal(self) -> None:
        assert change_summary(_compared(pages_added=1)) == "1 page added."

    def test_pluralizes_by_its_own_number(self) -> None:
        assert change_summary(_compared(pages_added=4)) == "4 pages added."

    def test_two_signals_are_joined_with_and(self) -> None:
        summary = change_summary(_compared(pages_added=2, pages_removed=1))
        assert summary == "2 pages added and 1 page removed."

    def test_three_signals_use_a_serial_comma_free_list(self) -> None:
        summary = change_summary(_compared(pages_added=2, pages_removed=1, content_changed=True))
        assert summary == "2 pages added, 1 page removed and page content changed."

    def test_a_change_with_no_countable_signal_is_honest_about_it(self) -> None:
        """`index_changed` said yes for a reason this function has no count for.

        The message must not claim a count it does not have, and must not be empty — an empty
        commit message body is worse than a vague one.
        """
        assert change_summary({"index_diff": {"state": "compared"}}) == (
            "The generated index changed."
        )

    def test_an_unreadable_shape_falls_back_rather_than_raising(self) -> None:
        for stats in (None, {}, {"index_diff": "nope"}):
            assert change_summary(stats) == "First generated index for this site."

    def test_the_summary_is_bounded(self) -> None:
        """It lands in a commit message, and `index_diff` can carry site-chosen text."""
        assert len(change_summary(_compared(pages_added=10**40))) <= MAX_SUMMARY_CHARS
