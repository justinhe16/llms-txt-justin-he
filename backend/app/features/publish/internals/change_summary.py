"""Reading a run's own diff to answer two questions a publication needs: did the index change,
and what should the commit message say?

Pure — no I/O, no clock, no settings. One input (`runs.stats`) and one output.

**Why this reads `stats` defensively rather than taking a typed value.** `runs.stats` is jsonb
whose shape belongs to the crawl feature and has changed twelve times (`RUN_STATS_VERSION`). This
module is in a different feature, so it reads that blob exactly the way `frontend/lib/crawls/`
reads it — key by key, checking types, defaulting rather than raising. The alternative, importing
`app.features.crawl.internals.index_diff`, is forbidden by ARCHITECTURE.md §3.1 (no feature
imports another feature's `internals/`) and would also be worse: it would couple a publication's
commit message to a module that is rewritten whenever the diff gains a signal.

**The bias, stated once: when in doubt, publish.** Every unreadable or missing key resolves to
"changed". A publication that happens when nothing changed costs a `skipped_unchanged` row — the
repository's own copy is compared before anything is written (`PublishService._publish_to_github`),
so a false "changed" is caught one step later and writes nothing. A false "unchanged" silently
stops publishing and the user never finds out. The two errors are not symmetric, so the default
is not either.
"""

from typing import Any, Final


MAX_SUMMARY_CHARS: Final = 300
"""How long the generated summary line may be.

It becomes part of a git commit message and a pull request body, so it is bounded for the same
reason every other externally-derived string in this codebase is. Sample page titles from the
crawled site can appear in it by way of `index_diff`, and those are chosen by the site rather than
by us.
"""

_FIRST_RUN_SUMMARY: Final = "First generated index for this site."


def index_changed(stats: dict[str, Any] | None) -> bool:
    """Whether this run's index differs from the previous completed run's.

    `True` for every state that is not an explicit "nothing changed", including a missing
    `index_diff`, an unrecognized shape, and a first run — see the module docstring on why the
    default leans toward publishing.
    """
    diff = _diff(stats)
    if diff is None:
        return True

    # A first run has no previous index to compare against, so it always publishes.
    if diff.get("state") != "compared":
        return True

    # Any one of these being true means the artifact is not what it was. `metadata_changed` is
    # read as changed when it is not comparable (an enrichment mode flip), which is the same
    # publish-when-in-doubt bias applied to the one signal that can be explicitly unknown.
    signals = (
        _count(diff, "pages_added"),
        _count(diff, "pages_removed"),
    )
    if any(signal > 0 for signal in signals):
        return True
    return bool(diff.get("metadata_changed")) or bool(diff.get("content_changed"))


def change_summary(stats: dict[str, Any] | None) -> str:
    """One human-readable line about what changed, for a commit message and a PR body.

    Factual and countable, with no adjectives — the same discipline `internals/llms_txt.py`'s
    blockquote follows. This text lands in someone else's repository history, where a claim we
    cannot substantiate is worse than a plain count.
    """
    diff = _diff(stats)
    if diff is None or diff.get("state") != "compared":
        return _FIRST_RUN_SUMMARY

    added, removed = _count(diff, "pages_added"), _count(diff, "pages_removed")
    parts: list[str] = []
    if added:
        parts.append(f"{added} page{_s(added)} added")
    if removed:
        parts.append(f"{removed} page{_s(removed)} removed")
    if diff.get("metadata_changed"):
        parts.append("titles or descriptions changed")
    if diff.get("content_changed"):
        parts.append("page content changed")

    if not parts:
        # Reachable when `index_changed` returned `True` for a reason this function has no count
        # for — an unrecognized diff shape, most likely. Honest rather than silent: the artifact
        # differs, and we are not claiming to know how.
        return "The generated index changed."
    return f"{_join(parts)}."[:MAX_SUMMARY_CHARS]


def _diff(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    """`stats["index_diff"]`, if it is a plain object."""
    if not isinstance(stats, dict):
        return None
    diff = stats.get("index_diff")
    return diff if isinstance(diff, dict) else None


def _count(diff: dict[str, Any], key: str) -> int:
    """A non-negative integer from the diff, or `0`. Never raises on a jsonb surprise."""
    value = diff.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _s(count: int) -> str:
    return "" if count == 1 else "s"


def _join(parts: list[str]) -> str:
    """ "a", "a and b", "a, b and c" — sentence-cased at the front by the caller's context."""
    if len(parts) == 1:
        return parts[0].capitalize()
    head = ", ".join(parts[:-1])
    return f"{head} and {parts[-1]}".capitalize()
