"""Tests for `app.features.crawl.internals.llms_txt` — the two artifacts a run publishes.

No database, no network, no clock: both generators are pure, and every test here calls them
directly with hand-built `CrawledPage` lists. `tests/test_url_ranking.py` and
`tests/test_crawl_extract.py` are the closest siblings in shape — this suite follows the same
pattern: one test (or a parametrized group) per named behaviour, named for what it asserts
rather than how.

This file was `test_llms_txt_stub.py` until PER-179, and was a one-bullet-per-page dump's own
test suite until the curated-index ticket. The determinism tests are the ones that have
survived both rewrites unchanged in spirit: "a shuffled list produces byte-identical output"
is still the property everything else here is built on top of.

**Every page's `markdown` is unique by default (`_page`'s own docstring).** Dedup now
fingerprints on the page body alone (`_fingerprint`), so two fixtures that happened to share
one canned paragraph would silently collapse into one entry — a footgun the old stub-era
`_BODY` constant did not have to guard against. Tests that mean to exercise dedup pass an
explicit, deliberately SHARED `markdown=` to two pages.
"""

import inspect
import random
import re
from datetime import UTC, datetime

import pytest

from app.features.crawl.internals import llms_txt
from app.features.crawl.internals.enrich import PageSummary, apply_summaries
from app.features.crawl.internals.llms_txt import (
    MAX_FULL_TEXT_BYTES,
    MAX_MAIN_BODY_LINKS,
    MAX_PAGE_TEXT_BYTES,
    MAX_TEXT_CHARS,
    OPTIONAL_SECTION,
    IndexCounts,
    PageSignals,
    count_full_txt_truncations,
    count_indexed_pages,
    generate_llms_full_txt,
    generate_llms_txt,
    rank_pages,
)
from app.features.crawl.schemas import CrawledPage


_SITE = "https://example.test"
"""The registered site every page in this module's fixtures belongs to — the `site_url`
both artifacts are built for. One constant rather than a literal per call so a test that
means to use a DIFFERENT origin (the off-origin exclusion cases) is visibly doing so."""


# Long enough to be a plausible non-empty extraction: `internals/extract.py` sets `is_empty`
# below `MIN_BODY_CHARS` (200), so a helper defaulting to `is_empty=False` needs a body that
# would genuinely have cleared that bar.
_BODY_PREFIX = (
    "This paragraph stands in for a real page's extracted markdown. It is comfortably longer "
    "than the two hundred characters `extract.py` requires before it stops calling a page "
    "empty, so a page built with it is one the extractor would have accepted rather than one "
    "this suite merely asserts about. "
)


def _page(
    url: str,
    *,
    title: str | None = None,
    description: str | None = None,
    markdown: str | None = None,
    is_empty: bool = False,
    status: int = 200,
) -> CrawledPage:
    """A `CrawledPage` with only the fields these two functions read spelled out.

    `markdown` defaults to `_BODY_PREFIX` plus a clause naming `url` itself — UNIQUE per call
    site, deliberately, since `_fingerprint` (dedup) now hashes the body alone: two pages built
    with the same literal string would silently collapse into one entry, which is exactly the
    bug this default exists to make hard to write by accident. A test that means to exercise
    dedup passes an explicit, shared `markdown=` to both pages under test.

    `is_empty=False` by default, the opposite of `tests/test_crawl_payload.py`'s helper:
    almost every case here is about a page that IS indexed, and the skip rule gets its own
    tests rather than being the default every other test has to opt out of.
    """
    return CrawledPage(
        url=url,
        status=status,
        title=title,
        content="",
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_bytes=0,
        description=description,
        markdown=markdown if markdown is not None else f"{_BODY_PREFIX}Unique to {url}.",
        is_empty=is_empty,
        blocked_reason=None,
    )


def _section_of(output: str, heading: str) -> list[str]:
    """Every bullet under `## {heading}`, up to the next H2."""
    lines = output.splitlines()
    start = lines.index(f"## {heading}") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            body.append(line)
    return body


def _headings(output: str) -> list[str]:
    return [line[3:] for line in output.splitlines() if line.startswith("## ")]


def _bullets(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("- ")]


def _main_body(output: str) -> str:
    """Everything before `## Optional`, or the whole document when there is none."""
    return output.split("\n## Optional\n", 1)[0]


def _assert_shape_invariant(output: str, *, bullets: bool) -> None:
    """The one property `internals/llms_txt.py`'s module docstring pins for ANY input, on
    BOTH artifacts — see that docstring's own "The shape invariant" section, which this
    function is the executable form of:

    * exactly one H1, and it is the first line;
    * exactly one blockquote, immediately after it;
    * no heading inside the free-prose block (the lines between the blockquote and the first
      `## `);
    * at least one `## ` section whenever at least one page is indexed (a bullet, in `llms.txt`,
      exists);
    * no `## ` section with nothing under it;
    * at most one `## Optional`, and it is last;
    * the document ends in exactly one trailing newline.

    Args:
        bullets: Whether "nothing under a `## ` heading" means "no `- ` bullet"
            (`llms.txt`, whose sections hold bullet lists) or "no non-blank line at all"
            (`llms-full.txt`, whose `## ` headings are per-PAGE headings followed by prose,
            never bullets).
    """
    assert output.endswith("\n"), "must end in a trailing newline"
    assert not output.endswith("\n\n"), "must end in EXACTLY one trailing newline"

    lines = output.splitlines()
    heading_indexes = [index for index, line in enumerate(lines) if line.startswith("## ")]
    # The HEADER region — H1, blockquote, free-prose block — is the only part of the
    # document this module fully controls the content of. Everything from the first `## `
    # onward is either bullets (`llms.txt`) or a page's own raw markdown body
    # (`llms-full.txt`), and a page's own body is free to contain further `#`/`##` lines as
    # ITS OWN content without that being a violation of THIS document's structure — so the
    # "no stray heading" checks below are scoped to the header, not the whole document.
    header_end = heading_indexes[0] if heading_indexes else len(lines)

    h1_lines = [index for index, line in enumerate(lines[:header_end]) if line.startswith("# ")]
    assert h1_lines == [0], f"exactly one H1, and it is the first line (found at {h1_lines})"

    blockquote_lines = [
        index for index, line in enumerate(lines[:header_end]) if line.startswith("> ")
    ]
    assert blockquote_lines == [2], (
        f"exactly one blockquote, immediately after the H1 (found at {blockquote_lines})"
    )

    for line in lines[3:header_end]:
        assert not line.startswith("#"), f"no heading inside the free-prose block: {line!r}"

    has_bullet = any(line.startswith("- ") for line in lines)
    if has_bullet:
        assert heading_indexes, "at least one `## ` section whenever a page is indexed"

    optional_indexes = [i for i in heading_indexes if lines[i] == "## Optional"]
    assert len(optional_indexes) <= 1, "at most one `## Optional`"
    if optional_indexes:
        assert heading_indexes[-1] == optional_indexes[0], "`## Optional` must be last"

    for position, start in enumerate(heading_indexes):
        end = heading_indexes[position + 1] if position + 1 < len(heading_indexes) else len(lines)
        body = lines[start + 1 : end]
        if bullets:
            assert any(line.startswith("- ") for line in body), (
                f"no `## ` section with zero bullets: {lines[start]!r}"
            )
        else:
            assert any(line.strip() for line in body), (
                f"no `## ` heading with nothing at all after it: {lines[start]!r}"
            )


def _assert_both_artifacts_satisfy_the_shape_invariant(
    pages: list[CrawledPage], *, site_url: str = _SITE
) -> None:
    generate_llms_txt(pages, site_url=site_url)  # never raises
    _assert_shape_invariant(generate_llms_txt(pages, site_url=site_url), bullets=True)
    _assert_shape_invariant(generate_llms_full_txt(pages, site_url=site_url), bullets=False)


# --- purity, and the language the stub left behind ---------------------------------------


def test_the_module_performs_no_io_no_network_and_reads_no_clock() -> None:
    """The seam's oldest promise, and the one a future edit is most likely to break quietly:
    both artifacts must be a pure function of the pages handed in. Asserted over the source
    rather than by mocking, the same way `tests/test_url_ranking.py` asserts it, because the
    failure being guarded against is an import someone added — not a call some test happened
    to exercise."""
    source = inspect.getsource(llms_txt)

    assert "async " not in source
    assert "await " not in source
    assert "httpx" not in source
    assert "requests" not in source
    assert "socket" not in source
    assert "open(" not in source
    assert "Path(" not in source
    assert "datetime.now(" not in source
    assert ".utcnow(" not in source
    assert "time.time(" not in source
    assert "time.monotonic(" not in source
    assert "import time" not in source
    # The caps here are format invariants of the artifact, not operational knobs — reading
    # them from settings would make a pure function configuration-dependent.
    assert "app.core.settings" not in source


def test_neither_the_module_nor_its_output_calls_itself_a_placeholder() -> None:
    """PER-179's most visible acceptance criterion. The stub's blockquote announced itself as
    a placeholder and pointed at an undesigned milestone; a real artifact that still said so
    would be worse than one that said nothing."""
    assert "placeholder" not in inspect.getsource(llms_txt).lower()

    pages = [_page("https://example.test/docs/a", title="A", description="First.")]
    assert "placeholder" not in generate_llms_txt(pages, site_url=_SITE).lower()
    assert "placeholder" not in generate_llms_full_txt(pages, site_url=_SITE).lower()
    assert "placeholder" not in generate_llms_txt([], site_url=_SITE).lower()


# --- the llmstxt.org shape ----------------------------------------------------------------


def test_the_document_has_exactly_one_h1_and_exactly_one_blockquote() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/guide/b", title="B"),
        _page("https://example.test/other/c", title="C"),
    ]
    lines = generate_llms_txt(pages, site_url=_SITE).splitlines()

    assert sum(1 for line in lines if line.startswith("# ")) == 1
    assert sum(1 for line in lines if line.startswith("> ")) == 1


def test_the_h1_names_the_sites_own_title_when_the_root_page_carries_one() -> None:
    pages = [
        _page("https://example.test/", title="Acme Docs"),
        _page("https://example.test/docs/a", title="Configuration | Acme Docs"),
    ]

    assert generate_llms_txt(pages, site_url=_SITE).splitlines()[0] == "# Acme Docs"


def test_the_h1_falls_back_to_the_origin_when_no_root_page_has_a_title() -> None:
    """A deep page's title describes that page, not the site, so it is never promoted to the
    H1 — a bare origin is obviously an identifier, where a subpage's name on the whole
    artifact is a quiet mislabelling."""
    pages = [
        _page("https://example.test/api/z", title="Z Endpoint"),
        _page("https://example.test/docs/a", title="Configuration"),
    ]

    assert generate_llms_txt(pages, site_url=_SITE).splitlines()[0] == "# https://example.test"


def test_the_root_pages_title_names_the_site_even_when_that_page_was_empty() -> None:
    """The JavaScript-shell case. `extract.py` deliberately keeps a shell's `<title>`, and a
    documentation SPA whose homepage is a mount div is exactly why: the page is skipped from
    the index, but it is still the only page that knows what the site is called."""
    pages = [
        _page("https://example.test/", title="Acme Docs", markdown="", is_empty=True),
        _page("https://example.test/docs/a", title="A"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert output.splitlines()[0] == "# Acme Docs"
    assert _section_of(output, "Docs") == ["- [A](https://example.test/docs/a)"]
    assert "## Other" not in output, "the empty root page is still skipped from the index"


def test_the_blockquote_is_the_root_pages_own_description_not_a_page_count() -> None:
    """The external-review finding this ticket exists to fix: the old blockquote described the
    GENERATOR ("an index of N pages"). The new one describes the SITE, in its own words, when
    it has any to draw from."""
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="Acme helps engineering teams publish documentation their users read.",
        ),
        _page("https://example.test/docs/a", title="A"),
    ]
    blockquote = generate_llms_txt(pages, site_url=_SITE).splitlines()[2]

    assert blockquote == ("> Acme helps engineering teams publish documentation their users read.")


def test_the_blockquote_falls_back_to_the_root_pages_first_markdown_paragraph() -> None:
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            markdown="Acme is a small tools company.\n\nWe make things.",
        )
    ]
    blockquote = generate_llms_txt(pages, site_url=_SITE).splitlines()[2]

    assert blockquote == "> Acme is a small tools company."


def test_the_blockquote_falls_back_to_a_count_sentence_with_no_root_page_metadata() -> None:
    """The last resort, not the norm — reached only when there is no root page, or the root
    page has neither a description nor a usable markdown paragraph. The old "Excludes N pages
    with no extractable content" clause is gone entirely: that number lives in
    `runs.stats["pages_empty_content"]` now."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", markdown="", is_empty=True),
    ]
    blockquote = generate_llms_txt(pages, site_url=_SITE).splitlines()[2]

    assert blockquote == "> An index of 1 page from https://example.test."
    assert "Excludes" not in generate_llms_txt(pages, site_url=_SITE)


def test_a_bullet_is_a_titled_link_annotated_with_the_pages_description() -> None:
    pages = [_page("https://example.test/docs/a", title="Configuration", description="How to.")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [Configuration](https://example.test/docs/a): How to." in output


def test_a_page_with_no_description_gets_a_bullet_with_no_trailing_colon() -> None:
    """A description is optional in the llmstxt.org shape, and a dangling `: ` is not an
    empty description — it is a broken line."""
    pages = [_page("https://example.test/docs/a", title="Configuration")]

    assert "- [Configuration](https://example.test/docs/a)\n" in generate_llms_txt(
        pages, site_url=_SITE
    )


def test_a_page_with_no_title_is_labelled_from_its_last_path_segment() -> None:
    pages = [_page("https://example.test/docs/getting-started")]

    assert "- [Getting Started](https://example.test/docs/getting-started)" in (
        generate_llms_txt(pages, site_url=_SITE)
    )


def test_a_page_with_no_title_drops_a_filename_extension_from_its_label() -> None:
    pages = [_page("https://example.test/docs/intro.html")]

    assert "- [Intro](https://example.test/docs/intro.html)" in generate_llms_txt(
        pages, site_url=_SITE
    )


def test_a_titleless_root_page_is_labelled_with_its_host() -> None:
    pages = [_page("https://example.test/", markdown="")]

    assert "- [example.test](https://example.test/)" in generate_llms_txt(pages, site_url=_SITE)


def test_titles_are_collapsed_onto_one_line() -> None:
    """Extraction reads a title out of markup where a newline is insignificant. Left alone, it
    would end the bullet and turn the remainder into a stray paragraph."""
    pages = [_page("https://example.test/docs/a", title="Configuration\n  |  Acme")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [Configuration | Acme](https://example.test/docs/a)" in output


def test_a_whitespace_only_title_falls_back_rather_than_emitting_an_empty_link() -> None:
    pages = [_page("https://example.test/docs/intro", title="   \n  ")]

    assert "- [Intro](https://example.test/docs/intro)" in generate_llms_txt(pages, site_url=_SITE)


def test_bracket_characters_in_a_title_are_escaped_so_the_link_survives() -> None:
    pages = [_page("https://example.test/docs/a", title="Arrays [and] slices")]

    assert "- [Arrays \\[and\\] slices](https://example.test/docs/a)" in generate_llms_txt(
        pages, site_url=_SITE
    )


def test_parentheses_in_a_url_are_encoded_so_the_link_survives() -> None:
    """Rare — `url_normalize.py` has been over every URL that reaches here — but a single
    unescaped `)` silently truncates the link target and corrupts the rest of the line."""
    pages = [_page("https://example.test/docs/f(x)", title="F")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [F](https://example.test/docs/f%28x%29)" in output


# --- sections: the canonical taxonomy ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/product/a", "Product"),
        ("/pricing", "Product"),
        ("/docs/a", "Docs"),
        ("/doc/a", "Docs"),
        ("/documentation/a", "Docs"),
        ("/guide/a", "Guides"),
        ("/guides/a", "Guides"),
        ("/getting-started/a", "Guides"),
        ("/api/a", "API"),
        ("/reference/a", "Reference"),
        ("/research/a", "Research & Data"),
        ("/vs/acme", "Comparisons"),
        ("/customers/a", "Customers"),
        ("/about", "Company"),
        ("/blog/a", "Blog"),
    ],
)
def test_a_url_segment_matching_the_canonical_taxonomy_wins_that_section(
    path: str, section: str
) -> None:
    """The curated table exists for exactly this: a page's SECTION is decided by its URL, not
    by humanizing a segment `_SECTION_NAMES` never heard of."""
    pages = [
        _page(f"https://example.test{path}", title="T"),
        # A second page in the same section, so consolidation cannot fold it into `Other`
        # and this test stays about taxonomy matching, not about the singleton-section rule.
        _page(f"https://example.test{path}/sibling", title="Sibling"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert f"## {section}" in output


def test_taxonomy_matching_is_on_any_path_segment_not_only_the_leading_one() -> None:
    """The same choice `internals/url_ranking.py`'s `_DOC_SEGMENT_NAMES` makes for itself: a
    site that nests its docs under a product name should not be penalized for where the `docs`
    segment happens to sit."""
    pages = [
        _page("https://example.test/help/docs/setup", title="Setup"),
        _page("https://example.test/help/docs/config", title="Config"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## Docs" in output


def test_taxonomy_matching_reads_hyphenated_sub_words_of_a_compound_segment() -> None:
    """Found on a real site (step 9 follow-up): a compound, hyphenated slug like
    `/reports-guides/` never matches this table under EXACT whole-segment matching, even
    though it is built from the same words the table already looks for — it was landing in
    `Other` purely because the site spells two taxonomy words as one segment. Still URL-only:
    this widens WHICH PART of the URL is read, not what field."""
    pages = [
        _page("https://example.test/vs-competitor/a", title="A"),
        _page("https://example.test/vs-competitor/b", title="B"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## Comparisons" in output


def test_a_new_url_word_added_for_a_real_site_resolves_to_its_section() -> None:
    """`enterprise` and `instructions` were added to the taxonomy after step 9 found genuine,
    generically-applicable gaps (a B2B site's `/enterprise` page, a site's own
    `/ai-instructions` page) — pinned here so a future edit cannot silently drop either."""
    pages = [
        _page("https://example.test/enterprise/a", title="A"),
        _page("https://example.test/enterprise/b", title="B"),
    ]
    assert "## Product" in generate_llms_txt(pages, site_url=_SITE)

    pages = [
        _page("https://example.test/setup-instructions/a", title="A"),
        _page("https://example.test/setup-instructions/b", title="B"),
    ]
    assert "## Guides" in generate_llms_txt(pages, site_url=_SITE)


def test_always_optional_reads_hyphenated_sub_words_of_a_compound_segment() -> None:
    """The always-Optional rules need the identical sub-word widening for the identical
    reason: a real site's own `/vulnerability-reporting/` page (found on step 9's Profound
    crawl) is exactly the case `vulnerability-disclosure` already names, spelled with a
    hyphen the exact-segment form alone would never catch."""
    pages = [
        _page("https://example.test/vulnerability-reporting", title="Report a Vulnerability"),
        _page("https://example.test/docs/a", title="A"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert any("Report a Vulnerability" in bullet for bullet in _section_of(output, "Optional"))
    assert "Report a Vulnerability" not in _main_body(output)


def test_taxonomy_matching_reads_only_the_url_never_the_title_or_label() -> None:
    """The enrichment-invariance rule, pinned directly at the unit this table is: a page whose
    TITLE happens to contain a taxonomy keyword must not be pulled into that section on the
    strength of its label alone — enrichment can rewrite a label on some pages of a run and
    not others, and a selection decision may never depend on that."""
    pages = [
        _page("https://example.test/misc/a", title="API Reference Guide"),
        _page("https://example.test/misc/b", title="API Reference Guide 2"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## API" not in output
    assert "## Misc" in output


def test_the_origin_root_page_lands_in_overview_and_nowhere_else() -> None:
    """The fix for "the homepage is filed under Other," pinned directly. Checked BEFORE the
    taxonomy table, so a root page whose title contains a taxonomy keyword is not pulled into
    that section instead."""
    pages = [_page("https://example.test/", title="Acme API Platform")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## Overview" in output
    assert "## API" not in output
    assert _section_of(output, "Overview") == ["- [Acme API Platform](https://example.test/)"]


@pytest.mark.parametrize("path", ["/sitemap.xml", "/-/"])
def test_a_page_with_no_readable_leading_segment_lands_in_other(path: str) -> None:
    pages = [
        _page(f"https://example.test{path}", title="T"),
        _page(f"https://example.test{path}/2", title="T2"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## Other" in output


def test_a_derived_section_name_colliding_with_optional_folds_to_other() -> None:
    """Spec gap 1.5.4: `/optional/pricing` would otherwise humanize to a second `## Optional`
    heading, which `parse_index` would misattribute to the demoted section."""
    pages = [
        _page("https://example.test/optional/a", title="A"),
        _page("https://example.test/optional/b", title="B"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert output.count("## Optional") <= 1
    assert "## Other" in output


def test_a_derived_section_surviving_alone_folds_into_other() -> None:
    """Consolidation: a DERIVED (non-canonical) section that ends up with exactly one entry in
    the main body folds into `Other` — what kills "Profound Black Friday Index" as an H2 with
    one bullet under it. A CANONICAL section with one entry is never folded — see the next
    test."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/some-unusual-section/only-page", title="Lonely"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## Some Unusual Section" not in output
    assert "Lonely" in _section_of(output, "Other")[0]


def test_a_canonical_section_with_one_entry_is_never_folded() -> None:
    pages = [_page("https://example.test/api/a", title="A")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "## API" in output
    assert "## Other" not in output


def test_sections_are_ordered_canonical_first_then_derived_alphabetically_then_other() -> None:
    pages = [
        _page("https://example.test/", title="Root"),
        _page("https://example.test/zebra/a", title="Z1"),
        _page("https://example.test/zebra/b", title="Z2"),
        _page("https://example.test/api/a", title="A"),
        _page("https://example.test/blog/a", title="B"),
        _page("https://example.test/docs/a", title="D"),
        _page("https://example.test/guide/a", title="G"),
    ]
    headings = _headings(_main_body(generate_llms_txt(pages, site_url=_SITE)))

    assert headings == ["Overview", "Docs", "Guides", "API", "Blog", "Zebra"]


def test_pages_within_a_section_are_ranked_not_alphabetized() -> None:
    """Spec §7: rank order within a section, not alphabetical — a shallower, seed-linked page
    can outrank a deeper one regardless of its URL."""
    pages = [
        _page("https://example.test/docs/z", title="Z"),
        _page("https://example.test/docs/a/deep/nested", title="Deep A"),
    ]
    signals = {
        "https://example.test/docs/a/deep/nested": PageSignals(
            linked_from_seed=True, sitemap_priority=None, lastmod=None
        )
    }
    output = generate_llms_txt(pages, site_url=_SITE, signals=signals)

    assert _section_of(output, "Docs") == [
        "- [Deep A](https://example.test/docs/a/deep/nested)",
        "- [Z](https://example.test/docs/z)",
    ]


# --- rank_pages ------------------------------------------------------------------------------


def test_rank_pages_returns_the_same_urls_generate_llms_txt_lists_in_the_same_order() -> None:
    """`rank_pages` shares `_select` with the generator it is meant to bias
    `internals/enrich.py`'s concurrency toward — this pins that the two cannot disagree: the
    URL order of `rank_pages`' own output matches the bullet order `generate_llms_txt` (main
    body then Optional) actually renders."""
    pages = [
        _page("https://example.test/docs/z", title="Z"),
        _page("https://example.test/", title="Acme"),
        _page("https://example.test/privacy", title="Privacy Policy"),
        _page("https://example.test/docs/a", title="A"),
    ]

    ranked = rank_pages(pages, site_url=_SITE)
    output = generate_llms_txt(pages, site_url=_SITE)

    bullet_urls = re.findall(r"\]\((\S+?)\)", output)
    assert [page.url for page in ranked] == bullet_urls


def test_rank_pages_drops_a_page_that_would_never_reach_the_artifact() -> None:
    """A page `_select` excludes entirely — here, empty content — is simply absent from
    `rank_pages`' own output, the same way it is absent from the rendered artifact; there is
    nothing to bias `enrich_pages` toward for a page that will never be listed."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", markdown="", is_empty=True),
    ]

    ranked = rank_pages(pages, site_url=_SITE)

    assert [page.url for page in ranked] == ["https://example.test/docs/a"]


def test_rank_pages_biases_toward_the_seed_linked_page_under_the_cap() -> None:
    """The concrete scenario `rank_pages` exists for: a page the seed itself links to should
    be handed to `enrich_pages` ahead of an unlinked page, because it is the one more likely
    to survive the main-body cap and lead the artifact."""
    linked = _page("https://example.test/blog/special", title="Special")
    unlinked = _page("https://example.test/blog/ordinary", title="Ordinary")
    signals = {linked.url: PageSignals(linked_from_seed=True, sitemap_priority=None, lastmod=None)}

    ranked = rank_pages([unlinked, linked], site_url=_SITE, signals=signals)

    assert [page.url for page in ranked] == [linked.url, unlinked.url]


def test_rank_pages_signals_none_matches_signals_omitted() -> None:
    pages = _mixed_pages()

    assert rank_pages(pages, site_url=_SITE) == rank_pages(pages, site_url=_SITE, signals=None)
    assert rank_pages(pages, site_url=_SITE) == rank_pages(pages, site_url=_SITE, signals={})


# --- always-Optional rules -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/privacy",
        "/terms",
        "/cookies",
        "/legal/dpa",
        "/brand/assets",
        "/careers",
        "/blog/2019/announcement",
        "/tag/tutorials",
        "/author/jane",
        "/changelog",
    ],
)
def test_always_optional_urls_are_demoted_regardless_of_rank(path: str) -> None:
    pages = [
        _page(f"https://example.test{path}", title="Always Optional"),
        _page("https://example.test/docs/a", title="A"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert any("Always Optional" in bullet for bullet in _section_of(output, "Optional"))
    assert "Always Optional" not in _main_body(output)


def test_always_optional_matching_reads_only_the_url_never_the_title() -> None:
    """The enrichment-invariance rule again, for stage-1's own rules: a page's demotion must
    not hinge on a label a model could rewrite for one page and not another."""
    pages = [
        _page("https://example.test/misc/x", title="Read Our Privacy Policy Today"),
        _page("https://example.test/misc/y", title="Read Our Privacy Policy Today 2"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "Read Our Privacy Policy Today" in _main_body(output)


def test_a_seed_linked_always_optional_page_still_demotes() -> None:
    """`ALWAYS_OPTIONAL_PENALTY` must outweigh even the strongest positive term
    (`SEED_LINK_BOOST`) — the arithmetic `ALWAYS_OPTIONAL_PENALTY`'s own docstring states."""
    pages = [
        _page("https://example.test/privacy", title="Privacy Policy"),
        _page("https://example.test/docs/a", title="A"),
    ]
    signals = {
        "https://example.test/privacy": PageSignals(
            linked_from_seed=True, sitemap_priority=1.0, lastmod=None
        )
    }
    output = generate_llms_txt(pages, site_url=_SITE, signals=signals)

    assert any("Privacy Policy" in bullet for bullet in _section_of(output, "Optional"))


# --- the main-body cap, and never an empty main body -----------------------------------------


def test_the_main_body_is_capped_at_max_main_body_links() -> None:
    pages = [
        _page(f"https://example.test/docs/{index:03d}", title=f"D{index}")
        for index in range(MAX_MAIN_BODY_LINKS + 10)
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert len(_bullets(_main_body(output))) == MAX_MAIN_BODY_LINKS
    assert counts.main == MAX_MAIN_BODY_LINKS
    assert counts.optional == 10
    assert "## Optional" in output


def test_a_seed_linked_page_survives_the_cap_over_an_unlinked_shallower_page() -> None:
    pages = [
        _page(f"https://example.test/blog/post-{index:02d}", title=f"Post {index}")
        for index in range(MAX_MAIN_BODY_LINKS)
    ]
    linked = _page("https://example.test/blog/deep/special", title="Special")
    pages.append(linked)
    signals = {linked.url: PageSignals(linked_from_seed=True, sitemap_priority=None, lastmod=None)}

    output = generate_llms_txt(pages, site_url=_SITE, signals=signals)

    assert "Special" in _main_body(output)


def test_a_section_emptied_entirely_by_the_cap_does_not_render() -> None:
    pages = [
        _page(f"https://example.test/docs/{index:03d}", title=f"D{index}")
        for index in range(MAX_MAIN_BODY_LINKS)
    ]
    pages.append(_page("https://example.test/blog/only", title="Blog Only"))
    output = generate_llms_txt(pages, site_url=_SITE)

    # Docs (weight 4.0) always outranks a lone Blog (weight 0.5) page at equal depth, so the
    # cap demotes the Blog page entirely and its heading must not appear with nothing under it.
    assert any("Blog Only" in bullet for bullet in _section_of(output, "Optional"))
    assert "## Blog" not in _main_body(output)


def test_the_main_body_is_never_left_empty_when_pages_survive_selection() -> None:
    """The floor to the cap's ceiling: a site that is entirely legal pages and archives still
    gets a main body naming its best few pages, never H1-blockquote-straight-to-Optional."""
    pages = [
        _page("https://example.test/privacy", title="Privacy Policy"),
        _page("https://example.test/terms", title="Terms of Service"),
        _page("https://example.test/cookies", title="Cookie Policy"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert counts.main == 3
    assert counts.optional == 0
    assert len(_bullets(_main_body(output))) == 3
    _assert_shape_invariant(output, bullets=True)


def test_the_floor_promotes_at_most_the_main_body_cap() -> None:
    pages = [
        _page(f"https://example.test/privacy/{index:03d}", title=f"Legal {index}")
        for index in range(MAX_MAIN_BODY_LINKS + 5)
    ]
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert counts.main == MAX_MAIN_BODY_LINKS
    assert counts.optional == 5


# --- the free-prose block ----------------------------------------------------------------------


def test_the_prose_block_carries_the_root_pages_first_paragraph() -> None:
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="A short blockquote sentence about Acme.",
            markdown=(
                "This is the root page's own opening paragraph, distinct from the "
                "description above, and long enough to survive the word cap applied to it."
            ),
        ),
        _page("https://example.test/docs/a", title="A"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    lines = output.splitlines()

    assert lines[2] == "> A short blockquote sentence about Acme."
    assert "This is the root page's own opening paragraph" in lines[4]


def test_the_prose_block_skips_a_markdown_heading_and_uses_the_next_paragraph() -> None:
    """Found on a real site (step 9): `internals/extract.py`'s markdown sometimes keeps a
    page's own `# Title¶` (a permalink-anchored H1) as its first block. Returning that
    verbatim would put a line starting with `#` inside the free-prose block, which the module
    docstring's shape invariant forbids — this pins the fix, `_first_paragraph` skipping any
    ATX-heading-shaped block rather than returning it."""
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            markdown=(
                "# Acme[¶](https://example.test/#acme)\n\n"
                "The real opening paragraph, long enough to survive the word cap that follows."
            ),
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "The real opening paragraph" in output
    assert "[¶]" not in output, "the heading-shaped block must not survive into the prose block"
    _assert_shape_invariant(output, bullets=True)


def test_a_marketing_paragraph_starting_with_a_hash_number_is_not_mistaken_for_a_heading() -> None:
    """`_ATX_HEADING_PATTERN` is deliberately narrower than "starts with `#`" — real marketing
    copy ("#1 platform for...") is not a heading and must survive into the prose block."""
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            markdown="#1 platform trusted by teams everywhere for getting real work done daily.",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "#1 platform trusted by teams" in output


def test_the_prose_block_omits_a_paragraph_near_identical_to_the_blockquote() -> None:
    """Sites routinely lift `og:description` from their own hero copy — repeating it one
    paragraph later would be noise, not orientation.

    This docstring used to say the text "still legitimately appears a SECOND time, as the root
    page's own bullet description." It does not any more: `_describe`'s blockquote check drops
    that copy too, so the sentence appears exactly ONCE in the whole document. Pinned below,
    because the count is the point.
    """
    text = "Acme helps teams ship documentation their users actually read every single day."
    pages = [_page("https://example.test/", title="Acme", description=text, markdown=text)]
    output = generate_llms_txt(pages, site_url=_SITE)
    lines = output.splitlines()

    assert lines[2] == f"> {text}"
    assert lines[3] == ""
    assert lines[4] == "## Overview", "no prose-block paragraph between blockquote and heading"
    assert output.count(text) == 1, "blockquote only — not the prose block, not the bullet"


def test_an_optional_section_adds_no_prose_about_the_document_itself() -> None:
    """The inverse of the test this replaces. A previous revision emitted a fixed sentence
    explaining the main-body/Optional convention whenever a run had an `## Optional` section;
    it is deleted, and this pins that a run WITH Optional gets no header prose a run without it
    would not also get. See the module docstring's "one free-prose slot" paragraph for why the
    slot is reserved for the site rather than for the document.

    Neither page here carries a description or markdown, so `_prose_paragraph` has nothing to
    offer either run — which makes the presence of `## ` immediately after the blockquote the
    whole assertion.
    """
    with_optional = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/privacy", title="Privacy Policy"),
    ]
    without_optional = [_page("https://example.test/docs/a", title="A")]

    for pages in (with_optional, without_optional):
        lines = generate_llms_txt(pages, site_url=_SITE).splitlines()
        assert lines[2].startswith("> ")
        assert lines[3] == ""
        assert lines[4] == "## Docs", "blockquote goes straight to the first heading"

    both = generate_llms_txt(with_optional, site_url=_SITE)
    assert f"## {OPTIONAL_SECTION}" in both, "the Optional section itself is untouched"
    assert "grouped under Optional" not in both
    assert "grouped by topic" not in both


def test_the_root_bullet_drops_a_description_that_restates_the_blockquote() -> None:
    """The structural case: `_site_sentence` and `_describe` read the same field, so the root
    page's bullet used to repeat the blockquote verbatim whenever it had a description at all.
    A bare link is the clean outcome the llmstxt.org format is built for — the sentence is still
    three lines up."""
    text = "Acme helps teams ship documentation their users actually read every single day."
    pages = [
        _page("https://example.test/", title="Acme", description=text, markdown="Body. " * 40),
        _page("https://example.test/docs/a", title="A", description="How to install the CLI."),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert f"> {text}" in output
    assert "- [Acme](https://example.test/)\n" in output, "bare link, no description"
    assert output.count(text) == 1
    # A page with something of its own to say is untouched.
    assert "- [A](https://example.test/docs/a): How to install the CLI." in output


def test_the_root_bullet_is_unconditionally_bare_when_the_homepage_has_a_description() -> None:
    """A consequence worth stating outright, because it surprised the author of the check: for
    the ROOT page the two texts cannot diverge, so its bullet now never carries a description.

    `_site_sentence` and `_describe` both reduce the root page's `description` to its first whole
    sentence under a 30-word cap and both pass it through `_clean` — same input, same transform,
    same output. So "drop the bullet when it duplicates the blockquote" is, on the root page
    alone, equivalent to "the root bullet has no description," and the three descriptions below
    (short, longer, one whose first sentence overruns the cap) all land the same way.

    Kept as a duplicate check rather than hardcoded as "root bullets are bare" on purpose: the
    check states WHY, so if `_site_sentence`'s cap or sourcing ever changes such that the two
    texts genuinely differ, a differing description would correctly survive instead of being
    stripped by a rule that had quietly stopped being true.
    """
    for description in (
        "Acme is a documentation platform.",
        "Acme is a documentation platform for engineering teams that care about their docs.",
        " ".join(["Acme"] * 40) + ".",
    ):
        pages = [
            _page(
                "https://example.test/",
                title="Acme",
                description=description,
                markdown="Every team ships docs. Few ship docs anyone reads. We fix that.",
            )
        ]
        output = generate_llms_txt(pages, site_url=_SITE)

        assert "- [Acme](https://example.test/)\n" in output, description
        assert "- [Acme](https://example.test/):" not in output, description


def test_a_non_root_bullet_keeps_a_description_the_blockquote_does_not_already_carry() -> None:
    """The check is a duplicate test, not a rule against bullets having descriptions. Only the
    root page's is structurally doomed; every other page speaks for itself."""
    pages = [
        _page("https://example.test/", title="Acme", description="Acme is a docs platform."),
        _page("https://example.test/docs/a", title="A", description="How to install the CLI."),
        _page("https://example.test/docs/b", title="B", description="How to configure the CLI."),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "> Acme is a docs platform." in output
    assert "- [A](https://example.test/docs/a): How to install the CLI." in output
    assert "- [B](https://example.test/docs/b): How to configure the CLI." in output


def test_the_blockquote_check_catches_a_prefix_not_only_an_exact_match() -> None:
    """`_is_near_identical`'s normalized-prefix case, which is the common shape rather than the
    exotic one: a blockquote capped at `_SITE_SENTENCE_MAX_WORDS` against a bullet description
    cut to its first whole sentence, both drawn from the same underlying copy."""
    sentence = "Acme helps teams ship documentation their users actually read."
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description=f"{sentence} It has done so since 2019 for hundreds of teams worldwide.",
            markdown="Body. " * 40,
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert f"> {sentence}" in output
    assert "- [Acme](https://example.test/)\n" in output, "prefix match still drops the bullet"


def test_a_count_sentence_blockquote_disables_the_check_rather_than_dropping_everything() -> None:
    """When the root page is absent, `_site_sentence` is `None` and the blockquote is a count
    sentence. Nothing is being duplicated, so every bullet keeps its own description — a `None`
    must not be compared against and must not silently match."""
    pages = [
        _page("https://example.test/docs/a", title="A", description="How to install the CLI."),
        _page("https://example.test/docs/b", title="B", description="How to configure the CLI."),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "> An index of 2 pages" in output
    assert "- [A](https://example.test/docs/a): How to install the CLI." in output
    assert "- [B](https://example.test/docs/b): How to configure the CLI." in output


def test_llms_full_txt_drops_the_duplicate_description_on_the_same_page() -> None:
    """Both artifacts are built from the one `_index_entries` list, so the check cannot apply to
    the index and not the expansion — which is exactly the disagreement `_select`'s docstring
    says sharing that list exists to prevent."""
    text = "Acme helps teams ship documentation their users actually read every single day."
    pages = [_page("https://example.test/", title="Acme", description=text, markdown="Body. " * 40)]

    index = generate_llms_txt(pages, site_url=_SITE)
    full = generate_llms_full_txt(pages, site_url=_SITE)

    assert index.count(text) == 1
    assert full.count(text) == 1


def test_no_prose_block_means_blockquote_goes_straight_to_the_first_heading() -> None:
    pages = [_page("https://example.test/docs/a", title="A")]
    lines = generate_llms_txt(pages, site_url=_SITE).splitlines()

    assert lines[2].startswith("> ")
    assert lines[3] == ""
    assert lines[4] == "## Docs"


def test_the_prose_block_skips_a_tagline_with_no_sentence_terminator() -> None:
    """Found on a real site: a homepage's first markdown block is routinely a hero-strip
    tagline ("Used by the best marketers in the world") with no sentence terminator at all —
    real text, but not a paragraph. `_prose_paragraph` must move on to the next block rather
    than using it or giving up. A distinct `description` keeps the blockquote's own sourcing
    out of this test — otherwise the tagline would legitimately surface there instead, via
    `_site_sentence`'s own (deliberately unqualified) markdown fallback."""
    tagline = "Used by the best marketers in the world"
    real_paragraph = "A real opening paragraph with an actual sentence that ends properly."
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="A short blockquote sentence, unrelated to the markdown below.",
            markdown=f"{tagline}\n\n{real_paragraph}",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert tagline not in output
    assert real_paragraph in output


def test_the_prose_block_skips_a_short_punctuated_fragment_below_the_minimum_length() -> None:
    """A terminator alone is not enough to qualify — "Sign up today." is a complete sentence
    and still not a substantial paragraph. A distinct `description` isolates the blockquote's
    own sourcing from this test, for the same reason the tagline test above needs one."""
    short_fragment = "Sign up today."
    real_paragraph = (
        "A real, complete opening paragraph long enough to qualify as substantial prose."
    )
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="A short blockquote sentence, unrelated to the markdown below.",
            markdown=f"{short_fragment}\n\n{real_paragraph}",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert short_fragment not in output
    assert real_paragraph in output


def test_the_prose_block_is_omitted_when_no_candidate_paragraph_qualifies() -> None:
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            markdown="Used by the best marketers in the world\n\nSign up today.\n\nGo",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "Sign up today." not in output
    assert "\nGo\n" not in output
    _assert_shape_invariant(output, bullets=True)


def test_the_prose_block_uses_the_whole_paragraph_when_it_fits() -> None:
    """A distinct `description` keeps the blockquote from being derived from this same
    paragraph — otherwise the blockquote's own (shorter) first-sentence summary would read as
    near-identical to the fuller paragraph and suppress the prose block entirely
    (`_is_near_identical`), which is correct behaviour but not what this test is about."""
    paragraph = "First sentence here. Second sentence follows naturally after it."
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="A short blockquote sentence, unrelated to the markdown below.",
            markdown=paragraph,
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert paragraph in output


def test_the_prose_block_falls_back_to_the_paragraphs_first_sentence_past_the_cap() -> None:
    """The three-tier rule: the whole paragraph is used only if it fits; otherwise its own
    first sentence, whole, never a word-boundary cut into the sentence that follows it. A
    distinct `description` keeps this test about the PROSE BLOCK specifically — without one,
    the blockquote's own fallback would independently pick up this same first sentence and
    make the prose block's own contribution ambiguous (near-identical suppression, correct
    behaviour, just not what this test means to isolate)."""
    first_sentence = "This is a short opening sentence that easily clears the qualifying bar."
    long_tail = " ".join(f"trailing{index}" for index in range(70))
    paragraph = f"{first_sentence} {long_tail}."
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            description="A short blockquote sentence, unrelated to the markdown below.",
            markdown=paragraph,
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert first_sentence in output
    assert "trailing0" not in output


def test_the_prose_block_never_truncates_inside_a_markdown_construct() -> None:
    """The unambiguous failure found on a real, small site: a naive word-boundary cut landed
    inside `[text](url)`, leaving a dangling, unterminated bracket in a document meant to be
    parsed ("…so you can [buy"). Constructed so the OLD word-boundary cut at the paragraph's
    word cap would have landed exactly on the opening `[` of a markdown link — the fix either
    emits the whole paragraph/sentence or omits it and moves on to the next candidate, never a
    fragment with an unbalanced construct."""
    filler = " ".join(f"filler{index}" for index in range(59))
    broken_candidate = (
        f"{filler} [buy expired domains](https://example.test/buy) today, right now, immediately."
    )
    good_candidate = "A real, complete opening paragraph that easily clears the qualifying bar."
    pages = [
        _page(
            "https://example.test/",
            title="Acme",
            markdown=f"{broken_candidate}\n\n{good_candidate}",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "[buy" not in output
    assert good_candidate in output
    _assert_shape_invariant(output, bullets=True)


# --- dedup -------------------------------------------------------------------------------------


def test_two_pages_sharing_a_body_collapse_to_the_higher_ranked_one() -> None:
    shared_body = f"{_BODY_PREFIX}Shared across two URLs on purpose, for this test alone."
    pages = [
        _page("https://example.test/docs/a", title="A", markdown=shared_body),
        _page("https://example.test/docs/b", title="B", markdown=shared_body),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert counts.main == 1
    assert counts.duplicate == 1
    # Equal rank on every other term; the URL tie-break keeps the alphabetically-first one.
    assert "docs/a" in output
    assert "docs/b" not in output


def test_dedup_runs_before_the_cap_so_a_duplicate_cannot_consume_a_slot() -> None:
    shared_body = f"{_BODY_PREFIX}Shared across every page in this test."
    pages = [
        _page(f"https://example.test/docs/{index:03d}", title=f"D{index}", markdown=shared_body)
        for index in range(MAX_MAIN_BODY_LINKS + 5)
    ]
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert counts.duplicate == MAX_MAIN_BODY_LINKS + 4
    assert counts.main == 1
    assert counts.optional == 0


def test_dedup_reads_the_body_only_never_the_label() -> None:
    """The bug this ticket fixes directly: two duplicate pages where one carries a
    model-written label and the other its extracted one must still fingerprint identically,
    because enrichment never touches `markdown`."""
    shared_body = f"{_BODY_PREFIX}Same body, different labels, for the dedup-label test."
    pages = [
        _page("https://example.test/docs/a", title="Extracted Title", markdown=shared_body),
        _page("https://example.test/docs/b", title="Model Written Title", markdown=shared_body),
    ]
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert counts.main == 1
    assert counts.duplicate == 1


# --- descriptions --------------------------------------------------------------------------


def test_a_description_is_cut_to_its_first_sentence() -> None:
    pages = [
        _page(
            "https://example.test/docs/a",
            title="A",
            description="First sentence here. Second sentence should not appear.",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "First sentence here." in output
    assert "Second sentence" not in output


def test_a_short_description_is_emitted_whole_with_its_own_punctuation_intact() -> None:
    pages = [
        _page(
            "https://example.test/docs/a",
            title="A",
            description="A short, honest sentence about this page.",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    bullet = _bullets(output)[0]

    assert bullet.split(": ", 1)[1] == "A short, honest sentence about this page."


def test_a_description_between_the_target_and_the_hard_cap_is_emitted_whole() -> None:
    """The "still reasonable as one line" tier: a first sentence that overruns the ~20-word
    target but fits within `_DESCRIPTION_MAX_WORDS` (30) is emitted WHOLE — never trimmed to
    20, which is exactly the old defect: a description ending in a dangling word with no
    terminal punctuation, indistinguishable from a truncated original."""
    words = " ".join(f"word{i}" for i in range(25)) + "."
    pages = [_page("https://example.test/docs/a", title="A", description=words)]
    output = generate_llms_txt(pages, site_url=_SITE)
    bullet = _bullets(output)[0]

    description = bullet.split(": ", 1)[1]
    assert description == words
    assert len(description.split()) == 25
    assert description.endswith(".")


def test_a_description_whose_first_sentence_overruns_the_hard_cap_is_dropped_entirely() -> None:
    """Never a word-boundary fragment: when the first sentence itself is longer than
    `_DESCRIPTION_MAX_WORDS`, the whole description is dropped and the bullet is a bare link
    — the clean outcome the llmstxt.org format is built to support, and strictly better than
    a description that ends mid-clause with no signal anything was cut."""
    words = " ".join(f"word{i}" for i in range(40))
    pages = [_page("https://example.test/docs/a", title="A", description=words)]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [A](https://example.test/docs/a)\n" in output
    assert ": " not in _bullets(output)[0]


def test_a_multi_sentence_description_over_the_cap_falls_back_to_its_first_sentence() -> None:
    """A first sentence that fits, followed by more text that would not — the ordinary case
    a real meta description produces. The description is exactly the first sentence, whole,
    never a word-boundary cut into the second one."""
    description = "This is a short opening sentence. " + " ".join(f"trailing{i}" for i in range(40))
    pages = [_page("https://example.test/docs/a", title="A", description=description)]
    output = generate_llms_txt(pages, site_url=_SITE)
    bullet = _bullets(output)[0]

    assert bullet.split(": ", 1)[1] == "This is a short opening sentence."


def test_a_cjk_description_stops_at_its_own_full_stop_not_the_whole_blob() -> None:
    """Found on a real site (step 9 follow-up): CJK prose does not put a space after its own
    sentence-ending punctuation (`。`), so the ASCII-only pattern never recognised one at all
    and a multi-sentence CJK description would have fallen through to "no terminator found,
    return everything" instead of stopping at its own first sentence."""
    description = "技術的な深掘り、製品のアップデート。これは二番目の文です、十分に長くなるように追加しています。"
    pages = [_page("https://example.test/docs/a", title="A", description=description)]
    output = generate_llms_txt(pages, site_url=_SITE)
    bullet = _bullets(output)[0]

    assert bullet.split(": ", 1)[1] == "技術的な深掘り、製品のアップデート。"


def test_no_emitted_bullet_description_is_a_mid_sentence_fragment() -> None:
    """The regression, measured the same way it was found: on real sites, roughly 28% of
    bullet descriptions ended with no terminal punctuation — cut mid-clause by the old
    word-boundary trim. Every description this module emits must now end in terminal
    punctuation (a real sentence, however the site itself chose to punctuate it) UNLESS it is
    the source's own complete, untouched text — which this synthetic battery cannot produce,
    because `_page`'s own fixture descriptions always end in a period. A single-word
    "sentence" is the one legitimate exception (`_first_sentence` finds no terminator and
    returns it whole, unmodified) and is excluded below by construction, not by exemption.
    """
    long_word_sentence = " ".join(f"{'x' * 40}{i}" for i in range(20)) + "."
    descriptions = [
        "A short, complete sentence about this page.",
        " ".join(f"word{i}" for i in range(19)) + ".",  # just under the target
        " ".join(f"word{i}" for i in range(20)) + ".",  # exactly the target
        " ".join(f"word{i}" for i in range(25)) + ".",  # between target and hard cap
        " ".join(f"word{i}" for i in range(30)) + ".",  # exactly the hard cap
        "First sentence is short. " + " ".join(f"w{i}" for i in range(50)),  # long trailer
        long_word_sentence,  # long WORDS, short word count
    ]
    pages = [
        _page(f"https://example.test/docs/{index:02d}", title=f"P{index}", description=text)
        for index, text in enumerate(descriptions)
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    fragments = [
        bullet
        for bullet in _bullets(output)
        if ": " in bullet and not bullet.rstrip().endswith((".", "!", "?", "\u2026"))
    ]
    assert fragments == []

    # And every description that DID survive is exactly its source's own first sentence — not
    # a coincidence of this particular battery having short descriptions, but the actual
    # invariant: nothing here was cut at a word boundary. Matched by URL, not position, since
    # rank order (not input order) decides where a bullet lands.
    bullet_by_url = {
        match.group(1): bullet
        for bullet in _bullets(output)
        if (match := re.match(r"^- \[.*?\]\((\S+?)\)", bullet))
    }
    for index, source in enumerate(descriptions):
        url = f"https://example.test/docs/{index:02d}"
        bullet = bullet_by_url[url]
        if ": " not in bullet:
            continue
        emitted = bullet.split(": ", 1)[1]
        # Through `_clean` too, not raw `_first_sentence` — a sentence within the word cap
        # can still exceed `MAX_TEXT_CHARS` (the `long_word_sentence` case above), and
        # `_clean`'s own safety-net cut is expected there, not a fragment this test should
        # flag; that cut is what appends the ellipsis `fragments` above already tolerates.
        assert emitted == llms_txt._clean(llms_txt._first_sentence(source.strip()))


def test_a_description_already_ending_in_an_ellipsis_is_dropped_entirely() -> None:
    """The site's OWN truncation — a bare link beats a broken sentence."""
    pages = [
        _page(
            "https://example.test/docs/a",
            title="A",
            description="This description was truncated by the site itself…",
        )
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [A](https://example.test/docs/a)\n" in output


def test_a_description_ending_in_three_dots_is_also_dropped() -> None:
    pages = [_page("https://example.test/docs/a", title="A", description="Truncated with dots...")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [A](https://example.test/docs/a)\n" in output


def test_clean_own_ellipsis_is_not_mistaken_for_the_sites() -> None:
    """Spec gap 1.5.3: the ellipsis check must run on the RAW description, before `_clean`'s
    own 500-char cut appends its own `…` — otherwise a genuinely long single sentence would be
    dropped as if the site had truncated it itself.

    Built from a handful of very long WORDS, not many short ones, so the sentence clears
    `_clean`'s 500-character safety net while staying at 20 words — comfortably under
    `_DESCRIPTION_MAX_WORDS` — which is what actually exercises `_clean`'s own cut rather than
    the (separate, more common) word-count drop `_whole_sentence` applies first."""
    long_word = "x" * 40
    long_sentence = " ".join(f"{long_word}{index}" for index in range(20)) + "."
    assert len(long_sentence) > MAX_TEXT_CHARS
    assert len(long_sentence.split()) <= 30

    pages = [_page("https://example.test/docs/a", title="A", description=long_sentence)]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [A](https://example.test/docs/a):" in output
    assert "…" in output


def test_a_description_that_merely_restates_the_label_is_dropped() -> None:
    pages = [
        _page("https://example.test/docs/a", title="Configuration", description="Configuration!")
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "- [Configuration](https://example.test/docs/a)\n" in output


def test_a_boilerplate_description_shared_by_three_or_more_pages_is_dropped_for_all() -> None:
    shared = "Acme is the best platform for teams who want to build great things quickly."
    pages = [
        _page(f"https://example.test/docs/{letter}", title=letter.upper(), description=shared)
        for letter in "abc"
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert shared not in output
    assert output.count(": ") == 0 or all(": " not in bullet for bullet in _bullets(output))


def test_a_description_shared_by_only_two_pages_is_not_boilerplate() -> None:
    """Two pages sharing a description is a two-page site telling the truth; three is a
    site-wide default."""
    shared = "Acme is the best platform for teams who want to build great things quickly."
    pages = [
        _page("https://example.test/docs/a", title="A", description=shared),
        _page("https://example.test/docs/b", title="B", description=shared),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert output.count(shared) == 2


def test_boilerplate_detection_runs_over_the_deduped_survivors() -> None:
    """So a duplicate page cannot manufacture a false boilerplate hit for a description shared
    by only two genuinely distinct pages."""
    shared_body = f"{_BODY_PREFIX}Shared body for the boilerplate-after-dedup test."
    shared_description = "Acme is the best platform for teams who want great things fast."
    pages = [
        _page(
            "https://example.test/docs/a",
            title="A",
            description=shared_description,
            markdown=shared_body,
        ),
        _page(
            "https://example.test/docs/b",
            title="B",
            description=shared_description,
            markdown=shared_body,
        ),
        _page("https://example.test/docs/c", title="C", description=shared_description),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    # docs/a and docs/b collapse to one (identical body) — only two SURVIVING pages share the
    # description, so it is not boilerplate and must survive.
    assert shared_description in output


# --- determinism ---------------------------------------------------------------------------


def _mixed_pages() -> list[CrawledPage]:
    return [
        _page("https://example.test/", title="Acme"),
        _page("https://example.test/api/b", title="B", description="Second."),
        _page("https://example.test/docs/a", title="A", description="First."),
        _page("https://example.test/docs/c", title="C"),
        _page("https://example.test/zebra/d", title="D"),
        _page("https://example.test/docs/e", markdown="", is_empty=True),
    ]


@pytest.mark.parametrize("seed", range(8))
def test_a_shuffled_pages_list_produces_byte_identical_output(seed: int) -> None:
    """THE contract this module has carried since it was a stub. `crawl_site`'s frontier
    fetches race each other, so the order pages arrive in is not reproducible between two runs
    of the same crawl; an artifact that depended on it would differ between two runs that
    fetched exactly the same pages."""
    pages = _mixed_pages()
    shuffled = list(pages)
    random.Random(seed).shuffle(shuffled)

    assert generate_llms_txt(shuffled, site_url=_SITE) == generate_llms_txt(pages, site_url=_SITE)
    assert generate_llms_full_txt(shuffled, site_url=_SITE) == generate_llms_full_txt(
        pages, site_url=_SITE
    )


@pytest.mark.parametrize("seed", range(4))
def test_a_shuffled_pages_list_is_still_deterministic_with_signals_present(seed: int) -> None:
    pages = _mixed_pages()
    signals = {
        "https://example.test/zebra/d": PageSignals(
            linked_from_seed=True, sitemap_priority=0.9, lastmod=datetime(2025, 6, 1, tzinfo=UTC)
        ),
        "https://example.test/docs/a": PageSignals(
            linked_from_seed=False, sitemap_priority=0.2, lastmod=datetime(2024, 1, 1, tzinfo=UTC)
        ),
    }
    shuffled = list(pages)
    random.Random(seed).shuffle(shuffled)

    assert generate_llms_txt(shuffled, site_url=_SITE, signals=signals) == generate_llms_txt(
        pages, site_url=_SITE, signals=signals
    )


def test_two_pages_scoring_identically_still_break_the_tie_on_url() -> None:
    """Every sort in the selection pipeline ends in `url` — the same guarantee, same
    mechanism, `select_urls` gives its own `(-score, url)` sort."""
    pages = [
        _page("https://example.test/docs/z", title="Z"),
        _page("https://example.test/docs/a", title="A"),
    ]

    first = generate_llms_txt(pages, site_url=_SITE)
    second = generate_llms_txt(list(reversed(pages)), site_url=_SITE)

    assert first == second
    assert _section_of(first, "Docs") == [
        "- [A](https://example.test/docs/a)",
        "- [Z](https://example.test/docs/z)",
    ]


def test_two_pages_sharing_a_url_cannot_reorder_the_output() -> None:
    """`crawl_site` visits each URL once, so this list is not one it can produce — but a
    stable sort keyed on the URL alone would leave these two in input order, which is the one
    way a shuffle could still change the output."""
    first = _page("https://example.test/docs/a", title="A")
    second = _page("https://example.test/docs/a", title="B")

    assert generate_llms_txt([first, second], site_url=_SITE) == generate_llms_txt(
        [second, first], site_url=_SITE
    )


def test_two_root_pages_sharing_a_url_cannot_change_the_project_name_or_blockquote() -> None:
    """The same hazard as above, on the OTHER two passes over `pages`. `_root_page` is shared
    by the H1 and the blockquote (spec's R8 risk) so the two cannot independently disagree
    about which duplicate root page won."""
    alpha = _page(
        "https://example.test/", title="Alpha", description="Alpha's own description sentence."
    )
    beta = _page(
        "https://example.test/", title="Beta", description="Beta's own description sentence."
    )

    forward = generate_llms_txt([alpha, beta], site_url=_SITE)
    backward = generate_llms_txt([beta, alpha], site_url=_SITE)

    assert forward == backward
    assert generate_llms_full_txt([alpha, beta], site_url=_SITE) == generate_llms_full_txt(
        [beta, alpha], site_url=_SITE
    )
    # Pinned further: the H1 and the blockquote must name the SAME winning page, not two
    # independently-resolved ones that merely happen to agree here.
    lines = forward.splitlines()
    assert lines[0] == "# Alpha"
    assert lines[2] == "> Alpha's own description sentence."


# --- the empty and all-empty cases ---------------------------------------------------------


def test_empty_input_returns_a_stable_document_rather_than_raising() -> None:
    assert generate_llms_txt([], site_url=_SITE) == generate_llms_txt([], site_url=_SITE)
    assert generate_llms_full_txt([], site_url=_SITE) == generate_llms_full_txt([], site_url=_SITE)
    assert "No pages were fetched" in generate_llms_txt([], site_url=_SITE)


def test_the_empty_document_names_the_origin_it_was_given() -> None:
    """`_empty_document` is a function of `site_url`, not a fixed constant — even a zero-page
    run knows which site it was about."""
    output = generate_llms_txt([], site_url=_SITE)

    assert output.splitlines()[0] == f"# {_SITE}"
    assert _SITE in output.splitlines()[2]


def test_the_empty_document_still_has_one_h1_and_one_blockquote() -> None:
    """Same SHAPE as a full artifact, so a consumer never has to special-case a run that
    fetched nothing. Never `""`, which in a `text` column is indistinguishable from a bug."""
    for output in (
        generate_llms_txt([], site_url=_SITE),
        generate_llms_full_txt([], site_url=_SITE),
    ):
        lines = output.splitlines()
        assert sum(1 for line in lines if line.startswith("# ")) == 1
        assert sum(1 for line in lines if line.startswith("> ")) == 1


def test_empty_pages_are_omitted_from_the_index() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B", markdown="", is_empty=True),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "https://example.test/docs/a" in output
    assert "https://example.test/docs/b" not in output


def test_a_run_whose_pages_are_all_empty_still_produces_a_document_with_no_sections() -> None:
    """Not the same case as `pages == []`: there is still an origin and a project name, so the
    header is real. What there must not be is a bare `## ` with nothing under it."""
    pages = [
        _page("https://example.test/", title="Acme", markdown="", is_empty=True),
        _page("https://example.test/docs/a", markdown="", is_empty=True),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)
    counts = count_indexed_pages(pages, site_url=_SITE)

    assert output.splitlines()[0] == "# Acme"
    assert "## " not in output
    assert counts == IndexCounts(main=0, optional=0, duplicate=0)


# --- IndexCounts -----------------------------------------------------------------------------


def test_count_indexed_pages_counts_only_the_pages_the_index_lists() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B"),
        _page("https://example.test/docs/c", markdown="", is_empty=True),
    ]

    assert count_indexed_pages(pages, site_url=_SITE) == IndexCounts(
        main=2, optional=0, duplicate=0
    )
    assert len(pages) == 3, "this is the divergence RUN_STATS_VERSION 3 records"


def test_count_indexed_pages_matches_the_bullets_the_artifact_actually_emits() -> None:
    """R1: `IndexCounts.main` equals the bullets before `## Optional`, `.optional` equals the
    bullets after it, and `main + optional` equals the `## ` heading count in
    `llms-full.txt` — the one implementation `_select` shares across every caller cannot
    disagree with itself."""
    pages = [
        _page(f"https://example.test/blog/{index:03d}", title=f"Post {index}")
        for index in range(MAX_MAIN_BODY_LINKS + 8)
    ]
    counts = count_indexed_pages(pages, site_url=_SITE)
    index_output = generate_llms_txt(pages, site_url=_SITE)
    full_output = generate_llms_full_txt(pages, site_url=_SITE)

    main_bullets = len(_bullets(_main_body(index_output)))
    optional_bullets = len(_bullets(index_output)) - main_bullets

    assert counts.main == main_bullets
    assert counts.optional == optional_bullets
    assert counts.main + counts.optional == full_output.count("\n## ")


# --- llms-full.txt --------------------------------------------------------------------------


def test_llms_full_txt_emits_a_heading_and_the_pages_markdown_for_each_page() -> None:
    pages = [_page("https://example.test/docs/a", title="Configuration", markdown="Body text.")]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "## Configuration" in output
    assert "Body text." in output


def test_llms_full_txt_follows_the_index_order_not_plain_url_order() -> None:
    """The two artifacts are read together, so a consumer that found a link in the index must
    find the bodies in the order the links were in. `/api/a` sorts before `/docs/b` by URL,
    but Docs precedes API by canonical section order, which is the ordering that wins."""
    pages = [
        _page("https://example.test/api/a", title="API Page"),
        _page("https://example.test/docs/b", title="Docs Page"),
    ]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert output.index("## Docs Page") < output.index("## API Page")


def test_llms_full_txt_carries_optional_pages_too() -> None:
    """Spec §8: `llms-full.txt` carries EVERYTHING, main body then Optional — a page demoted
    for exceeding the cap has not lost its content, only its prominence."""
    pages = [
        _page(
            f"https://example.test/docs/{index:03d}", title=f"D{index}", markdown=f"Body {index}."
        )
        for index in range(MAX_MAIN_BODY_LINKS + 1)
    ]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert output.count("\n## ") == MAX_MAIN_BODY_LINKS + 1


def test_llms_full_txt_never_emits_firecrawl_page_separators() -> None:
    """Firecrawl emits `<|firecrawl-page-N-lllmstxt|>` between pages and then strips them out
    again with a regex before anything consumes the result. Copying a marker whose own author
    removes it would be copying the bug."""
    output = generate_llms_full_txt(_mixed_pages(), site_url=_SITE)

    assert "firecrawl" not in output.lower()
    assert "<|" not in output


def test_llms_full_txt_omits_the_pages_the_index_omits() -> None:
    pages = [
        _page("https://example.test/docs/a", title="Kept", markdown="Kept body."),
        _page("https://example.test/docs/b", title="Skipped", markdown="", is_empty=True),
    ]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "## Kept" in output
    assert "## Skipped" not in output


def test_a_heading_in_the_full_text_is_not_bracket_escaped() -> None:
    """The same label is a markdown link text in the index and a heading here, and only the
    first needs its brackets escaped — a `\\[` in a heading renders literally."""
    pages = [_page("https://example.test/docs/a", title="Arrays [and] slices")]

    assert "## Arrays [and] slices" in generate_llms_full_txt(pages, site_url=_SITE)


# --- the caps -------------------------------------------------------------------------------


def test_a_page_over_the_per_page_cap_is_truncated_and_says_so() -> None:
    oversized = "x" * (MAX_PAGE_TEXT_BYTES + 5_000)
    pages = [_page("https://example.test/docs/a", title="Big", markdown=oversized)]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "Truncated" in output
    assert oversized not in output
    assert count_full_txt_truncations(pages, site_url=_SITE) == 1


def test_the_per_page_cap_is_measured_in_utf8_bytes_not_characters() -> None:
    """30 000 CJK characters are 30 000 characters and 90 000 bytes. A character budget would
    let this through at three times the column size it advertises."""
    body = "あ" * 30_000
    pages = [_page("https://example.test/docs/a", title="Big", markdown=body)]

    assert len(body) < MAX_PAGE_TEXT_BYTES < len(body.encode())
    assert count_full_txt_truncations(pages, site_url=_SITE) == 1


def test_truncation_never_splits_a_multibyte_character() -> None:
    """A cut landing mid-character must drop that character, not emit a lone continuation byte
    or a replacement character."""
    pages = [_page("https://example.test/docs/a", title="Big", markdown="あ" * 30_000)]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "�" not in output
    output.encode()  # would raise on a lone surrogate


def test_the_run_cap_stops_at_a_page_boundary_and_says_how_many_were_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-page cut could land inside a fenced code block and leave it unterminated, and
    would make a trimmed page indistinguishable from a complete one."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    bodies = [f"{index:02d}-" + "y" * 397 for index in range(10)]
    pages = [
        _page(f"https://example.test/docs/{index:02d}", title=f"P{index}", markdown=bodies[index])
        for index in range(10)
    ]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "Truncated" in output
    assert output.count(bodies[0]) == 1, "whole pages, never a partial one"
    included = output.count("\n## ")
    assert 0 < included < 10
    assert count_full_txt_truncations(pages, site_url=_SITE) == 10 - included


def test_the_run_cap_leaves_the_index_and_its_link_count_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two `llms-full.txt` caps bound the expansion, not the index — a run that could not
    inline every body still lists every page it fetched (up to the SEPARATE main-body cap)."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    pages = [
        _page(
            f"https://example.test/docs/{index:02d}",
            title=f"P{index}",
            markdown=f"{index:02d}-" + "y" * 397,
        )
        for index in range(10)
    ]

    counts = count_indexed_pages(pages, site_url=_SITE)
    assert counts.main == 10
    assert len(_bullets(generate_llms_txt(pages, site_url=_SITE))) == 10


def test_the_full_text_never_exceeds_the_run_cap_at_realistic_sizes() -> None:
    """The test that actually proves the column bound, at the sizes that would breach it: 150
    pages each at the per-page cap is 7.5 MiB of input against a 5 MiB budget."""
    pages = [
        _page(
            f"https://example.test/docs/{index:03d}",
            title=f"Page {index}",
            markdown="z" * (MAX_PAGE_TEXT_BYTES + 100),
        )
        for index in range(150)
    ]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert len(output.encode()) <= MAX_FULL_TEXT_BYTES
    assert count_full_txt_truncations(pages, site_url=_SITE) > 0


def test_full_txt_truncated_is_zero_when_every_page_fits() -> None:
    assert count_full_txt_truncations(_mixed_pages(), site_url=_SITE) == 0
    assert count_full_txt_truncations([], site_url=_SITE) == 0


def test_a_page_both_trimmed_and_then_dropped_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count of pages missing content, not of truncation events — otherwise the number
    exceeds the page count and stops meaning anything."""
    monkeypatch.setattr(llms_txt, "MAX_FULL_TEXT_BYTES", 2_000)
    pages = [
        _page(
            f"https://example.test/docs/{index}",
            title=f"P{index}",
            markdown=f"{index}-" + "w" * (MAX_PAGE_TEXT_BYTES + 100),
        )
        for index in range(4)
    ]

    assert count_full_txt_truncations(pages, site_url=_SITE) == len(pages)


def test_an_enormous_title_cannot_push_the_full_text_past_the_run_cap() -> None:
    """The crawled site chooses the `<title>`, and `extract.py` bounds neither it nor the
    description. Before `MAX_TEXT_CHARS`, a single page with an eight-megabyte title produced
    an eight-megabyte header before the first page section was even considered, so
    `MAX_FULL_TEXT_BYTES` never got a say and an unbounded value reached a Postgres column."""
    pages = [_page("https://example.test/", title="T" * (8 * 1024 * 1024))]

    assert len(generate_llms_full_txt(pages, site_url=_SITE).encode()) <= MAX_FULL_TEXT_BYTES
    assert len(generate_llms_txt(pages, site_url=_SITE).encode()) <= MAX_FULL_TEXT_BYTES


def test_an_enormous_description_cannot_bloat_the_index() -> None:
    """`llms.txt` has no size cap of its own — bullets were assumed small — so the bound on a
    description is what makes that assumption true, IN ADDITION TO the new ~20-word trim."""
    pages = [_page("https://example.test/docs/a", title="A", description="D" * (8 * 1024 * 1024))]

    assert len(generate_llms_txt(pages, site_url=_SITE).encode()) < 10_000


@pytest.mark.parametrize("field", ["title", "description"])
def test_an_over_long_title_or_description_is_cut_and_marked(field: str) -> None:
    # A single "word" with no spaces, so the ~20-word description trim cannot shrink it and
    # `_clean`'s MAX_TEXT_CHARS safety net is what has to catch it.
    pages = [_page("https://example.test/docs/a", **{field: "x" * 5_000})]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "…" in output
    assert len(max(output.splitlines(), key=len)) < MAX_TEXT_CHARS + 200


def test_a_title_at_the_limit_is_left_exactly_as_it_is() -> None:
    """The cut is a guard against a pathological page, not a house style — a value that fits
    must survive byte for byte, ellipsis included nowhere."""
    title = "T" * MAX_TEXT_CHARS
    pages = [_page("https://example.test/docs/a", title=title)]

    assert f"- [{title}](https://example.test/docs/a)" in generate_llms_txt(pages, site_url=_SITE)


def test_a_page_with_no_markdown_body_is_not_counted_as_truncated() -> None:
    """Only constructible by hand — `is_empty` is derived from the body's length — but nothing
    was cut, so nothing is reported as cut."""
    pages = [_page("https://example.test/docs/a", title="A", markdown="", is_empty=False)]
    output = generate_llms_full_txt(pages, site_url=_SITE)

    assert "## A" in output
    assert count_full_txt_truncations(pages, site_url=_SITE) == 0


# --- Exclusions that are not `is_empty` -------------------------------------------------
#
# Both regressions below were found by running the real pipeline against live sites, and both
# produced a WRONG artifact rather than a crash — which is why each pins the specific output
# that was observed, not merely "the page is absent."


def test_a_non_2xx_page_is_omitted_from_the_index() -> None:
    """A `404`/`429`/`5xx` body is real HTML with a real `<title>`, so `is_empty` does not
    catch it. Observed live: four `429 Too Many Requests` pages entered anthropic.com's index
    as ordinary bullets."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/rate-limited", title="Too Many Requests", status=429),
        _page("https://example.test/docs/gone", title="Not Found", status=404),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "https://example.test/docs/a" in output
    assert "Too Many Requests" not in output
    assert "Not Found" not in output
    assert count_indexed_pages(pages, site_url=_SITE) == IndexCounts(
        main=1, optional=0, duplicate=0
    )


def test_a_page_that_landed_on_another_host_is_omitted_from_the_index() -> None:
    """`CrawledPage.url` is the FINAL url after redirects, so a same-origin request can come
    back off-origin. Such a page is about a different site and does not belong in this one's
    index."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://cdn.example.net/assets/brochure", title="Brochure"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "https://example.test/docs/a" in output
    assert "cdn.example.net" not in output
    assert count_indexed_pages(pages, site_url=_SITE) == IndexCounts(
        main=1, optional=0, duplicate=0
    )


def test_one_off_origin_page_cannot_rename_the_whole_artifact() -> None:
    """**The `# https://claude.com` regression, pinned directly.**

    `_origin` used to be `min(page.url for page in pages)` — the alphabetically first URL the
    run collected. Here `https://cdn.example.net/...` sorts before `https://example.test/...`,
    so the old implementation titled the document after the CDN and then skipped the correct
    root page, whose origin no longer matched the one it had just derived. Exactly what one
    redirected page out of a hundred did to anthropic.com (`# https://claude.com`) and one CDN
    asset did to stripe.com (`# https://assets.ctfassets.net`).

    The site's own root page is deliberately listed LAST here, so a regression cannot pass by
    accident of ordering.
    """
    pages = [
        _page("https://cdn.example.net/assets/brochure", title="Brochure"),
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/", title="Acme Docs"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert output.splitlines()[0] == "# Acme Docs"
    assert "cdn.example.net" not in output


def test_the_artifact_describes_site_url_even_when_no_page_shares_its_origin() -> None:
    """The degenerate case the old derivation could not express at all: `site_url` is what the
    document claims, so an index of zero pages still names the right site rather than naming
    whichever host happened to answer."""
    pages = [_page("https://cdn.example.net/assets/brochure", title="Brochure")]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert output.splitlines()[0] == "# https://example.test"


def test_the_full_text_applies_the_same_exclusions_as_the_index() -> None:
    """The two artifacts are read together — a body in `llms-full.txt` with no link in
    `llms.txt` is exactly the disagreement `_index_entries` is shared to prevent."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/gone", title="Not Found", status=404),
        _page("https://cdn.example.net/assets/brochure", title="Brochure"),
    ]
    full = generate_llms_full_txt(pages, site_url=_SITE)

    assert "## A" in full
    assert "Not Found" not in full
    assert "Brochure" not in full


def test_apex_and_www_pages_belong_to_one_artifact() -> None:
    """`www` is not a different site, so neither spelling drops the other's pages. The
    blockquote still names the origin the run actually landed on — the DISPLAY form keeps its
    host (`_origin`), only the membership comparison folds (`_origin_key`)."""
    pages = [
        _page("https://www.example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B"),
    ]

    from_www = generate_llms_txt(pages, site_url="https://www.example.test")
    from_apex = generate_llms_txt(pages, site_url="https://example.test")

    for output in (from_www, from_apex):
        assert "https://www.example.test/docs/a" in output
        assert "https://example.test/docs/b" in output


def test_a_host_merely_starting_with_www_is_still_another_site() -> None:
    """The fold is one label, not a prefix match."""
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://www2.example.test/docs/b", title="B"),
    ]
    output = generate_llms_txt(pages, site_url=_SITE)

    assert "https://example.test/docs/a" in output
    assert "www2.example.test" not in output


# --- signals: PageSignals, None, missing keys (R4) --------------------------------------------


def test_signals_none_and_signals_empty_dict_are_byte_identical() -> None:
    pages = _mixed_pages()

    assert generate_llms_txt(pages, site_url=_SITE) == generate_llms_txt(
        pages, site_url=_SITE, signals={}
    )
    assert generate_llms_txt(pages, site_url=_SITE) == generate_llms_txt(
        pages, site_url=_SITE, signals=None
    )


def test_a_signals_map_covering_only_some_pages_degrades_the_rest_to_no_signal() -> None:
    pages = [
        _page("https://example.test/docs/a", title="A"),
        _page("https://example.test/docs/b", title="B"),
    ]
    partial_signals = {
        "https://example.test/docs/a": PageSignals(
            linked_from_seed=True, sitemap_priority=None, lastmod=None
        )
    }

    # Must not raise, and must produce a complete artifact listing both pages.
    output = generate_llms_txt(pages, site_url=_SITE, signals=partial_signals)
    assert "docs/a" in output
    assert "docs/b" in output


def test_an_explicit_sitemap_priority_of_one_half_scores_identically_to_none() -> None:
    """`SITEMAP_DEFAULT_PRIORITY` centers `priority` at 0.5 specifically so a site operator
    who wrote `<priority>0.5</priority>` on purpose and a candidate that omitted the tag score
    identically."""
    pages = [
        _page("https://example.test/blog/a", title="A"),
        _page("https://example.test/blog/b", title="B"),
    ]
    explicit_half = {
        "https://example.test/blog/a": PageSignals(
            linked_from_seed=False, sitemap_priority=0.5, lastmod=None
        )
    }

    with_explicit = generate_llms_txt(pages, site_url=_SITE, signals=explicit_half)
    without = generate_llms_txt(pages, site_url=_SITE)

    assert with_explicit == without


# --- selection is enrichment-invariant --------------------------------------------------------


def test_selection_is_identical_whether_or_not_enrichment_touched_a_page() -> None:
    """The invariant this ticket is built on: `internals/enrich.py` keeps PARTIAL results on
    its own wall-clock timeout (its own docstring's example — 80 of 100 pages summarized, the
    other 20 falling back to extraction — is the intended behaviour, not a degraded case), so
    a mix of model-written and extraction-derived labels inside one run is the common case.
    Selection — which pages are indexed, their section, the main-body/Optional split, their
    order, and every `IndexCounts` field — must be, and is, identical either way. Only the
    labels attached to each already-selected page may differ.
    """
    pages = [
        _page(f"https://example.test/docs/{index:02d}", title=f"Extracted {index}")
        for index in range(20)
    ]
    pages.append(_page("https://example.test/privacy", title="Extracted Privacy"))
    pages.append(_page("https://example.test/blog/post", title="Extracted Blog"))

    # An ARBITRARY SUBSET gets a model-written summary — every third page — mirroring a
    # partial enrichment pass rather than the all-or-nothing case.
    summaries = {
        page.url: PageSummary(title=f"Model title for {page.url}", description="Model description.")
        for index, page in enumerate(pages)
        if index % 3 == 0
    }
    enriched_pages = apply_summaries(pages, summaries)
    assert enriched_pages != pages, "the fixture must actually exercise a label rewrite"

    raw_counts = count_indexed_pages(pages, site_url=_SITE)
    enriched_counts = count_indexed_pages(enriched_pages, site_url=_SITE)
    assert raw_counts == enriched_counts

    raw_output = generate_llms_txt(pages, site_url=_SITE)
    enriched_output = generate_llms_txt(enriched_pages, site_url=_SITE)

    # Section assignment, identical per page — pull `(section, url)` pairs out of each parsed
    # document's bullet order within the main body, ignoring labels.
    def _section_and_url_pairs(output: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        section = ""
        for line in output.splitlines():
            if line.startswith("## "):
                section = line[3:]
                continue
            match = re.match(r"^- \[.*?\]\((\S+?)\)", line)
            if match:
                pairs.append((section, match.group(1)))
        return pairs

    assert _section_and_url_pairs(raw_output) == _section_and_url_pairs(enriched_output)

    # The URL order alone, main body then Optional.
    def _url_order(output: str) -> list[str]:
        return [url for _section, url in _section_and_url_pairs(output)]

    assert _url_order(raw_output) == _url_order(enriched_output)


# --- the shape invariant, over a matrix of degenerate inputs -----------------------------------


def test_shape_invariant_zero_pages() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant([])


def test_shape_invariant_one_page_the_root_only() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [_page("https://example.test/", title="Acme")]
    )


def test_shape_invariant_one_page_not_the_root() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [_page("https://example.test/docs/a", title="A")]
    )


def test_shape_invariant_every_page_is_empty() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page("https://example.test/", title="Acme", markdown="", is_empty=True),
            _page("https://example.test/docs/a", markdown="", is_empty=True),
        ]
    )


def test_shape_invariant_no_page_carries_a_description() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page("https://example.test/", title="Acme"),
            _page("https://example.test/docs/a", title="A"),
            _page("https://example.test/privacy", title="Privacy Policy"),
        ]
    )


def test_shape_invariant_no_page_carries_a_title() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page("https://example.test/"),
            _page("https://example.test/docs/a"),
            _page("https://example.test/docs/b"),
        ]
    )


def test_shape_invariant_the_root_page_is_missing_entirely() -> None:
    """A seed that redirected to `/en/` — the site has no page at the bare origin root."""
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page("https://example.test/en/", title="Acme EN"),
            _page("https://example.test/docs/a", title="A"),
        ]
    )


def test_shape_invariant_every_page_matches_an_always_optional_rule() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page("https://example.test/privacy", title="Privacy Policy"),
            _page("https://example.test/terms", title="Terms of Service"),
            _page("https://example.test/cookies", title="Cookie Policy"),
        ]
    )


def test_shape_invariant_every_page_is_in_one_section() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [_page(f"https://example.test/docs/{index:03d}", title=f"D{index}") for index in range(12)]
    )


def test_shape_invariant_every_page_is_in_a_different_section() -> None:
    """Consolidation sweeps nearly all of these into `Other`."""
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page(f"https://example.test/{segment}-{index}/only", title=f"P{index}")
            for index, segment in enumerate(
                ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
            )
        ]
    )


def test_shape_invariant_several_hundred_pages_so_the_cap_bites_hard() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [_page(f"https://example.test/docs/{index:04d}", title=f"D{index}") for index in range(400)]
    )


def test_shape_invariant_a_root_page_with_a_description_but_no_markdown() -> None:
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page(
                "https://example.test/",
                title="Acme",
                description="A short honest description of Acme in a few plain words.",
                markdown="",
                is_empty=False,
            )
        ]
    )


def test_shape_invariant_root_markdown_leads_with_its_own_heading() -> None:
    """Found on a real site (step 9): a page's own extracted markdown can begin with a
    permalink-anchored `# Title¶` heading — the exact input that broke the invariant before
    `_first_paragraph` learned to skip a heading-shaped block."""
    _assert_both_artifacts_satisfy_the_shape_invariant(
        [
            _page(
                "https://example.test/",
                title="Acme",
                markdown=(
                    "# Acme[¶](https://example.test/#acme)\n\n"
                    "A real opening paragraph long enough to survive the word cap."
                ),
            ),
            _page("https://example.test/docs/a", title="A"),
        ]
    )
