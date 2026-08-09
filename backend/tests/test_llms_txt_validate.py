"""Tests for `app.features.crawl.internals.validate` — checking an `llms.txt` against
llmstxt.org.

No database, no network, no clock: `validate_llms_txt` is pure and every test below calls it
directly on a hand-written document. `tests/test_llms_txt.py` (the generator these documents
imitate) and `tests/test_index_diff.py` (the other pure parser of this same format) are this
suite's two closest siblings in shape.

**`test_generated_index_is_clean` is the drift gate**, and it is the reason this module was
written to take a `str` rather than a `list[CrawledPage]`. `validate.py` and `llms_txt.py` are
two independent descriptions of one format with no shared code between them, so nothing but a
test can hold them together — and a test that fed the validator its own idea of a well-formed
document would prove only that the validator is self-consistent. That test runs the REAL
generator, over adversarial metadata, and requires zero findings of either severity. If it
fails, one of the two modules has moved and the diff says which.

The severity split is asserted deliberately throughout: `conforms` tracks ERRORS only, because
llmstxt.org requires nothing but an H1 (see `validate.py`'s module docstring). Several tests
below therefore assert `conforms is True` on a document they simultaneously assert has
warnings — that pairing is the contract, not an oversight.
"""

from datetime import UTC, datetime
from typing import Any

from app.features.crawl.internals.llms_txt import generate_llms_txt
from app.features.crawl.internals.validate import (
    MAX_FINDINGS,
    MAX_MESSAGE_EXCERPT_CHARS,
    VALIDATION_VERSION,
    validate_llms_txt,
)
from app.features.crawl.schemas import CrawledPage


_BODY = (
    "This paragraph stands in for a real page's extracted markdown. It is comfortably longer "
    "than the two hundred characters `extract.py` requires before it stops calling a page "
    "empty, so a page built with it is one the extractor would have accepted."
)

# A minimal document that passes everything: one H1, a summary directly after it, one H2, one
# well-formed item. Every "one thing is wrong" test below is this shape with one line changed,
# so a finding a test asserts on is provably caused by the line it changed and not by the
# scaffolding around it.
_VALID = """# Acme Docs

> An index of 1 page from https://acme.test.

## Docs

- [Configuration](https://acme.test/docs/config): How to configure Acme.
"""


def _page(
    url: str,
    *,
    title: str | None = None,
    description: str | None = None,
    markdown: str = _BODY,
    is_empty: bool = False,
) -> CrawledPage:
    """A `CrawledPage` with only the fields `generate_llms_txt` reads spelled out. Mirrors
    `tests/test_llms_txt.py`'s helper of the same name, including its `is_empty=False`
    default."""
    return CrawledPage(
        url=url,
        status=200,
        title=title,
        content="",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_bytes=0,
        description=description,
        markdown=markdown,
        is_empty=is_empty,
        blocked_reason=None,
    )


def _codes(report: dict[str, Any]) -> list[str]:
    return [finding["code"] for finding in report["findings"]]


def _find(report: dict[str, Any], code: str) -> dict[str, Any]:
    """The one finding with `code`, asserting there is exactly one.

    Exactly one rather than "the first", because a check firing twice on a single-fault
    document is itself the bug most of these tests would otherwise hide.
    """
    matches = [finding for finding in report["findings"] if finding["code"] == code]
    assert len(matches) == 1, f"expected exactly one {code!r}, got {_codes(report)}"
    return matches[0]


# ---------------------------------------------------------------------------------------
# The drift gate
# ---------------------------------------------------------------------------------------


def test_generated_index_is_clean() -> None:
    """The real generator's output has no findings — not even warnings.

    The adversarial metadata is the point: brackets and parentheses in titles exercise
    `llms_txt.py`'s `_escape_label`/`_escape_target` against this module's `_LINK_PATTERN`,
    which are the two halves most likely to drift apart, and a path that humanizes to nothing
    exercises the `Other` section. A clean report over THAT input is evidence the two modules
    agree; a clean report over `# Acme\\n` would not be.
    """
    pages = [
        _page("https://acme.test/", title="Acme Docs", description="The home page."),
        _page(
            "https://acme.test/docs/config",
            title="Configuration [advanced]",
            description="Options (and their defaults).",
        ),
        _page("https://acme.test/api/v1", title="API v1", description="The v1 endpoints."),
        _page("https://acme.test/sitemap.xml", title="Sitemap", description=None),
        _page("https://acme.test/guides/start", title="Getting started"),
        _page("https://acme.test/skipped", title="Skipped", is_empty=True),
    ]

    report = validate_llms_txt(generate_llms_txt(pages, site_url="https://acme.test"))

    assert report["findings"] == []
    assert report["conforms"] is True
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["structure"]["link_count"] == 5


def test_generated_empty_document_is_conformant() -> None:
    """The `pages == []` artifact conforms.

    `_EMPTY_DOCUMENT` is an H1 and a blockquote and nothing else, which the spec permits — so
    this asserts `conforms is True` alongside the `no_sections` warning that is the honest
    thing to say about an artifact with no links in it. Getting an ERROR here would mean a run
    that legitimately fetched nothing recorded a malformed artifact.
    """
    report = validate_llms_txt(generate_llms_txt([], site_url="https://acme.test"))

    assert report["conforms"] is True
    assert _codes(report) == ["no_sections"]
    assert report["structure"]["has_summary"] is True


# ---------------------------------------------------------------------------------------
# The H1 — the spec's one requirement
# ---------------------------------------------------------------------------------------


def test_valid_document_has_no_findings() -> None:
    report = validate_llms_txt(_VALID)

    assert report["findings"] == []
    assert report["conforms"] is True
    assert report["version"] == VALIDATION_VERSION
    assert report["structure"] == {
        "h1": "Acme Docs",
        "has_summary": True,
        "section_count": 1,
        "link_count": 1,
        "has_optional_section": False,
    }


def test_missing_h1_is_an_error() -> None:
    report = validate_llms_txt("> A summary with no title above it.\n")

    assert report["conforms"] is False
    finding = _find(report, "missing_h1")
    assert finding["severity"] == "error"
    assert finding["line"] is None, "an absence has no line to point at"


def test_empty_document_reports_missing_h1() -> None:
    """An empty string is a `missing_h1` error, never an empty findings list.

    "Nothing was checked" and "everything passed" must not be the same result — a caller
    reading `conforms` off a document that was never populated would otherwise be told it is
    fine.
    """
    for text in ("", "   \n\n  \n"):
        report = validate_llms_txt(text)
        assert report["conforms"] is False
        assert _codes(report)[0] == "missing_h1"


def test_second_h1_is_an_error() -> None:
    report = validate_llms_txt(_VALID + "\n# A Second Title\n")

    assert report["conforms"] is False
    assert _find(report, "multiple_h1")["line"] == 9
    assert report["structure"]["h1"] == "Acme Docs", "the first H1 stays the title"


def test_text_before_h1_is_an_error() -> None:
    report = validate_llms_txt("Some preamble prose.\n\n" + _VALID)

    assert report["conforms"] is False
    finding = _find(report, "content_before_h1")
    assert finding["line"] == 1
    assert "Some preamble prose." in finding["message"]


def test_blockquote_before_h1_is_an_error() -> None:
    """Ordering is a rule: the same blockquote that is correct after the H1 is a finding
    before it."""
    report = validate_llms_txt("> Summary first.\n\n# Acme Docs\n")

    assert report["conforms"] is False
    assert _find(report, "content_before_h1")["line"] == 1
    assert report["structure"]["has_summary"] is False


def test_bom_before_h1_is_permitted() -> None:
    """The spec allows a leading BOM, so it must not read as content before the H1."""
    report = validate_llms_txt("﻿" + _VALID)

    assert report["findings"] == []
    assert report["structure"]["h1"] == "Acme Docs"


def test_empty_h1_is_an_error() -> None:
    report = validate_llms_txt("# \n\n## Docs\n\n- [A](https://acme.test/a)\n")

    assert report["conforms"] is False
    assert _find(report, "empty_h1")["severity"] == "error"


def test_hash_without_space_is_not_a_heading() -> None:
    """`#hashtag` is not an ATX heading, so a document led by one has no H1."""
    report = validate_llms_txt("#hashtag\n")

    assert _codes(report)[0] == "content_before_h1"
    assert _find(report, "missing_h1")["severity"] == "error"


# ---------------------------------------------------------------------------------------
# Ordering: the summary and the prose section
# ---------------------------------------------------------------------------------------


def test_blockquote_after_prose_is_out_of_order() -> None:
    text = """# Acme Docs

Some prose about the project.

> A summary arriving too late.

## Docs

- [A](https://acme.test/a)
"""
    report = validate_llms_txt(text)

    assert report["conforms"] is False
    assert _find(report, "summary_out_of_order")["line"] == 5
    assert report["structure"]["has_summary"] is False


def test_blockquote_after_a_section_is_out_of_order() -> None:
    report = validate_llms_txt(_VALID + "\n> A trailing summary.\n")

    assert report["conforms"] is False
    assert _find(report, "summary_out_of_order")["line"] == 9


def test_multi_line_blockquote_is_one_summary() -> None:
    """The generator wraps its blockquote across lines, each prefixed `>`. Consecutive
    quote lines are one summary, and the second must not be read as arriving out of order."""
    text = """# Acme Docs

> An index of 12 pages from https://acme.test.
> Excludes 3 pages with no extractable content.

## Docs

- [A](https://acme.test/a)
"""
    report = validate_llms_txt(text)

    assert report["findings"] == []
    assert report["structure"]["has_summary"] is True


def test_prose_between_summary_and_sections_is_permitted() -> None:
    """The spec allows "zero or more markdown sections … of any type except headings" here,
    so prose is conformant — and so is a list, which it names explicitly."""
    text = """# Acme Docs

> A summary.

This file indexes the documentation. Interpret the sections below as follows:

- Not a file list, just prose.

## Docs

- [A](https://acme.test/a)
"""
    report = validate_llms_txt(text)

    assert report["findings"] == []
    assert report["structure"]["link_count"] == 1, "a prose list contributes no file links"


def test_missing_summary_is_a_warning_not_an_error() -> None:
    """The blockquote is optional per the spec, so its absence must not fail conformance."""
    report = validate_llms_txt("# Acme Docs\n\n## Docs\n\n- [A](https://acme.test/a)\n")

    assert report["conforms"] is True
    assert report["error_count"] == 0
    finding = _find(report, "no_summary")
    assert finding["severity"] == "warning"
    assert "optional" in finding["message"]


def test_heading_in_prose_is_an_error() -> None:
    """An H3 before the first H2 sits in the prose section, which excludes headings."""
    text = (
        "# Acme Docs\n\n> A summary.\n\n### A subheading\n\n## Docs\n\n- [A](https://acme.test/a)\n"
    )
    report = validate_llms_txt(text)

    assert report["conforms"] is False
    assert _find(report, "heading_in_prose")["line"] == 5


def test_heading_below_h2_is_only_a_warning() -> None:
    """The same H3, inside a file-list section, is undescribed rather than forbidden."""
    report = validate_llms_txt(_VALID + "\n### A sub-grouping\n\n- [B](https://acme.test/b)\n")

    assert report["conforms"] is True
    assert _find(report, "heading_below_h2")["severity"] == "warning"


# ---------------------------------------------------------------------------------------
# File-list sections and their items
# ---------------------------------------------------------------------------------------


def test_item_without_a_link_is_an_error() -> None:
    report = validate_llms_txt(_VALID + "- A bullet with no link at all.\n")

    assert report["conforms"] is False
    finding = _find(report, "item_without_link")
    assert finding["line"] == 8
    assert "A bullet with no link at all." in finding["message"]


def test_notes_require_a_colon() -> None:
    text = "# Acme Docs\n\n> S.\n\n## Docs\n\n- [A](https://acme.test/a) some notes\n"
    report = validate_llms_txt(text)

    assert report["conforms"] is False
    finding = _find(report, "malformed_item_notes")
    assert finding["line"] == 7
    assert "some notes" in finding["message"]


def test_notes_with_a_colon_are_conformant() -> None:
    report = validate_llms_txt(_VALID)

    assert report["findings"] == []


def test_link_with_no_notes_is_conformant() -> None:
    """The spec's notes are "optionally a `:` and notes", so a bare link is a complete
    item."""
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [A](https://acme.test/a)\n")

    assert report["findings"] == []


def test_dangling_colon_is_a_warning() -> None:
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [A](https://acme.test/a):\n")

    assert report["conforms"] is True
    assert _find(report, "empty_item_notes")["severity"] == "warning"


def test_empty_link_name_is_an_error() -> None:
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [](https://acme.test/a)\n")

    assert report["conforms"] is False
    assert _find(report, "empty_link_name")["line"] == 7


def test_empty_link_url_is_an_error_and_not_a_link() -> None:
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [A]()\n")

    assert report["conforms"] is False
    assert _find(report, "empty_link_url")["line"] == 7
    assert report["structure"]["link_count"] == 0, "an unfollowable item is not counted"
    assert _find(report, "no_links")["severity"] == "warning"


def test_relative_url_is_a_warning() -> None:
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [A](/docs/a)\n")

    assert report["conforms"] is True
    finding = _find(report, "relative_link_url")
    assert finding["severity"] == "warning"
    assert report["structure"]["link_count"] == 1, "relative but still an item with a target"


def test_scheme_relative_url_is_a_warning() -> None:
    """`//host/path` resolves against the document's own scheme, and an `llms.txt` in a
    model's context has none."""
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- [A](//acme.test/a)\n")

    assert _find(report, "relative_link_url")["severity"] == "warning"


def test_escaped_brackets_in_a_label_do_not_break_the_link() -> None:
    """The label pattern is escape-aware, matching `llms_txt.py`'s `_escape_label`."""
    text = "# Acme\n\n> S.\n\n## Docs\n\n- [Config \\[beta\\]](https://acme.test/a): Notes.\n"
    report = validate_llms_txt(text)

    assert report["findings"] == []
    assert report["structure"]["link_count"] == 1


def test_all_three_bullet_markers_are_recognized() -> None:
    """A third-party file may use `*` or `+`. An item this scanner misses is an item it
    cannot report on, which is the one failure mode a validator must not have."""
    for marker in ("-", "*", "+"):
        text = f"# Acme\n\n> S.\n\n## Docs\n\n{marker} A bullet with no link.\n"
        report = validate_llms_txt(text)
        assert _find(report, "item_without_link")["line"] == 7, f"marker {marker!r}"


def test_empty_section_is_a_warning() -> None:
    text = "# Acme\n\n> S.\n\n## Docs\n\n## API\n\n- [A](https://acme.test/a)\n"
    report = validate_llms_txt(text)

    assert report["conforms"] is True
    assert _find(report, "empty_section")["line"] == 5
    assert report["structure"]["section_count"] == 2


def test_trailing_empty_section_is_a_warning() -> None:
    """The last section closes at end-of-document rather than at the next H2, which is a
    separate code path from the one `test_empty_section_is_a_warning` covers."""
    report = validate_llms_txt(_VALID + "\n## Empty\n")

    assert _find(report, "empty_section")["line"] == 9


def test_prose_inside_a_section_is_a_warning() -> None:
    report = validate_llms_txt(_VALID + "Prose sitting inside the file list.\n")

    assert report["conforms"] is True
    finding = _find(report, "prose_in_section")
    assert finding["severity"] == "warning"
    assert "Prose sitting inside the file list." in finding["message"]


def test_no_sections_is_a_warning() -> None:
    """`# Acme` alone is fully conformant and completely useless — which is exactly the pair
    the two severities exist to express."""
    report = validate_llms_txt("# Acme Docs\n\n> A summary.\n")

    assert report["conforms"] is True
    assert _codes(report) == ["no_sections"]


def test_optional_section_is_reported_in_structure() -> None:
    """The one H2 the spec gives a meaning to. Reported, never a finding."""
    report = validate_llms_txt(_VALID + "\n## Optional\n\n- [Changelog](https://acme.test/c)\n")

    assert report["findings"] == []
    assert report["structure"]["has_optional_section"] is True
    assert report["structure"]["section_count"] == 2


def test_optional_section_match_is_exact() -> None:
    report = validate_llms_txt(_VALID.replace("## Docs", "## Optional Reading"))

    assert report["structure"]["has_optional_section"] is False


# ---------------------------------------------------------------------------------------
# Robustness of the scan itself
# ---------------------------------------------------------------------------------------


def test_crlf_endings_are_read_as_lines() -> None:
    """A CRLF document must not read as one giant line — the whole report would be wrong."""
    report = validate_llms_txt(_VALID.replace("\n", "\r\n"))

    assert report["findings"] == []
    assert report["structure"]["link_count"] == 1


def test_hash_inside_a_code_fence_is_not_a_heading() -> None:
    text = """# Acme Docs

> A summary.

```
# not a heading
```

## Docs

- [A](https://acme.test/a)
"""
    report = validate_llms_txt(text)

    assert report["findings"] == []
    assert report["structure"]["h1"] == "Acme Docs"


def test_findings_are_capped_but_counts_are_exact() -> None:
    """`MAX_FINDINGS` trims the list; `error_count` is counted before it does.

    A malformed document must not be able to put an unbounded list into `runs.stats`, and it
    must not be able to understate its own scale either.
    """
    bad_items = "".join(f"- A bullet with no link, number {index}.\n" for index in range(60))
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n" + bad_items)

    assert len(report["findings"]) == MAX_FINDINGS
    assert report["findings_truncated"] is True
    assert report["error_count"] == 60
    assert report["conforms"] is False


def test_clean_document_is_not_marked_truncated() -> None:
    report = validate_llms_txt(_VALID)

    assert report["findings_truncated"] is False


def test_long_offending_line_is_excerpted() -> None:
    """A finding's message quotes a bounded excerpt. The line being quoted was written by the
    site being crawled, and 25 unbounded quotes would go into a jsonb column."""
    report = validate_llms_txt("# Acme\n\n> S.\n\n## Docs\n\n- " + "x" * 5_000 + "\n")

    message = _find(report, "item_without_link")["message"]
    assert "…" in message
    assert len(message) < MAX_MESSAGE_EXCERPT_CHARS + 200
