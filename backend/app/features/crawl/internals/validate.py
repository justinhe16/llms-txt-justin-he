"""Does a generated `llms.txt` actually conform to llmstxt.org? A pure checker over the
artifact's own text.

Pure in the same sense `internals/llms_txt.py` next door is pure — no network, no clock, no
settings read — and for a sharper reason than symmetry: this module's whole job is to be an
INDEPENDENT description of the format. It takes a `str` and never a `list[CrawledPage]`, so it
cannot see how the artifact was built and cannot accidentally agree with the generator by
sharing its code. A validator that imported `_bullet` would only ever confirm that `_bullet`
equals itself. `tests/test_llms_txt_validate.py`'s round-trip test is the other half of that
argument: it runs the REAL generator's output through this checker and requires zero findings,
so the two implementations of one format are tied together by a test rather than by an import.

**It validates, and it does not repair.** Nothing here rewrites, normalizes, or re-emits an
artifact, and nothing downstream of it may either — a run whose index is malformed still
stores exactly the index it generated. The finding is the deliverable. If a check here starts
failing on our own output the fix belongs in `llms_txt.py`, which is the component that was
wrong; silently correcting the text at this seam would hide a generator bug behind a validator
that reports success.

## What the spec actually requires, and why almost everything is a warning

llmstxt.org is permissive to a degree worth stating plainly, because it is what shapes this
module: **the H1 is the only required element.** The blockquote summary is optional, the prose
section is optional, and the H2 file lists are optional. A file containing nothing but
`# Acme` is fully conformant and completely useless.

So a checker that only reported spec violations would return "conforms" for an artifact with
no links in it, which is not the question a user asking "is my llms.txt any good?" is asking.
Two severities keep both answers available without conflating them:

* **`error`** — the document violates something the spec states. `conforms` is `False`. These
  are the checks a consumer parsing to the spec could break on.
* **`warning`** — the spec permits it and it still makes the artifact worse. `conforms` stays
  `True`. A missing summary and a section with no links both live here, each saying so in its
  own message rather than leaving the reader to guess whether we invented the rule.

`conforms` is therefore exactly "no errors", never "no findings". A clean bill of health is
`conforms and not warning_count`, and callers that want to say "valid" should say which of the
two they mean.

## Ordering is a rule, not a convention

The spec lists its components "in order": BOM, H1, blockquote, prose, H2 sections. That makes
a blockquote sitting after a paragraph a violation rather than a stylistic choice, and it is
why `_scan` below is a single forward pass with a phase counter instead of five independent
searches — "did a blockquote appear anywhere" is answerable without position, and answers the
wrong question.

## Bounded output, because this lands in a jsonb column

`runs.stats` has no size cap (see `llms_txt.py`'s `MAX_FULL_TEXT_BYTES` docstring for the one
artifact that does), and a pathologically malformed 5,000-line document would otherwise
produce 5,000 findings inside a row every list query decodes. `MAX_FINDINGS` caps the list and
`findings_truncated` records that it bound, on the same reasoning
`internals/index_diff.py`'s `SAMPLE_LIMIT` uses: the counts stay exact and complete, only the
per-finding detail is trimmed.
"""

import re
from typing import Any, Final
from urllib.parse import urlsplit


VALIDATION_VERSION: Final = 1
"""Which definition of this module's returned shape a stored row was written under.

Deliberately its own number rather than a lean on `RUN_STATS_VERSION`. That version tracks
which KEYS `runs.stats` has, and it bumps for reasons that have nothing to do with this block
— it has moved twelve times already for crawler, enrichment and diff changes. A reader asking
"which checks produced this findings list?" needs to know when the CHECKS changed, which is a
strictly narrower question, and answering it from a number that also moves whenever a crawl
statistic is added would mean re-reading this module's history to tell the two apart.

Bump it when a check is added, removed, or has its severity changed — all three change what a
stored `findings` list means. Do not bump it for a reworded message.
"""

MAX_FINDINGS: Final = 25
"""How many findings the returned list carries, newest check order preserved.

25 rather than a round 10 because the errors a real malformed document produces cluster: one
missing `:` convention repeated across a section's bullets is a dozen findings that are all
the same mistake, and a cap that cut before the SECOND distinct problem would hide the more
useful half. `error_count` and `warning_count` are counted before this cap applies, so the
numbers never lie about scale even when the list is trimmed."""

MAX_MESSAGE_EXCERPT_CHARS: Final = 80
"""How much of an offending line a finding's message quotes.

Bounded for the reason `llms_txt.py`'s `MAX_TEXT_CHARS` bounds a title: the text being quoted
was chosen by the site being crawled, not by this codebase, and a page with an 8 KB
description would otherwise put 8 KB into a finding message — inside a jsonb column, 25 times
over."""

_EXCERPT_ELLIPSIS: Final = "…"

ERROR: Final = "error"
WARNING: Final = "warning"

_BOM: Final = "﻿"
"""The optional byte-order mark the spec permits before the H1. Stripped before scanning, so
a BOM-prefixed document is not reported as having content before its own title — which is
what a naive scanner would say about a perfectly conformant file."""

_FENCE_PATTERN: Final = re.compile(r"^\s{0,3}(?:```|~~~)")
"""A fenced code block's delimiter.

Present for input this project does not generate: `llms_txt.py` collapses every title and
description to a single line and prefixes every bullet with `- `, so a fence cannot appear in
our own output. It is handled anyway because a `#` inside a code fence is not a heading, and a
checker whose answer depends on who wrote the file is a checker with a blind spot rather than
a scope."""

_HEADING_PATTERN: Final = re.compile(r"^(#{1,6})(?:\s+(.*))?$")
"""An ATX heading and its level, with the title group `None` for a heading that has no title.

The whitespace is required only when a title FOLLOWS, which is what lets this match both
`# Acme` and a bare `#` while still rejecting `#hashtag` — after the hashes, `#hashtag` has
neither whitespace nor end-of-line, so it is not a heading and a document led by one has no
H1. Matching the titleless case matters: `# ` rstrips to `#`, and a pattern that missed it
would report an empty H1 as text appearing before a heading that is right there, which is a
diagnosis pointing at the wrong line."""

_LIST_ITEM_PATTERN: Final = re.compile(r"^\s*[-*+]\s+(.*)$")
"""A markdown list item, in any of the three bullet markers CommonMark allows and at any
indentation. Deliberately wider than `llms_txt.py`'s own `- ` output and wider than
`index_diff.py`'s `_BULLET_PATTERN`: those two describe what this project EMITS, and this one
has to recognize a list item a third-party file wrote in order to say what is wrong with it.
An item this pattern misses is not reported at all, which is the one failure mode a validator
must not have."""

_LINK_PATTERN: Final = re.compile(r"^\[((?:\\.|[^\]])*)\]\(([^)]*)\)(.*)$")
"""A markdown hyperlink at the start of a list item's content, plus whatever trails it.

The label group is escape-aware for the same reason `index_diff.py`'s `_BULLET_PATTERN` is —
`\\.` consumes an escaped pair as one unit so an escaped `\\]` inside a label does not end the
match early. The trailing group is captured rather than anchored away because the spec's
"optionally a `:` and notes" makes the text after the link a thing to CHECK, not a thing to
skip: `_check_item` below is what decides whether it is well-formed notes or a malformed
suffix."""

_OPTIONAL_SECTION: Final = "Optional"
"""The one H2 heading the spec gives a defined meaning: "the URLs provided there can be
skipped if a shorter context is needed". Reported in `structure` rather than checked — a
document may have one or not, and neither is a finding. It is surfaced because a consumer
deciding what to drop for a shorter context needs to know whether the artifact gave it that
affordance at all."""


def validate_llms_txt(text: str) -> dict[str, Any]:
    """Check `text` against llmstxt.org and return the `validation` block `runs.stats` stores.

    Args:
        text: A whole `llms.txt` document. Our own `generate_llms_txt` output in every caller
            today; written to accept anything, because the checks are about the format rather
            than about the producer.

    Returns:
        A JSON-safe dict:

        * `conforms` (bool) — no `error` findings. **Not** "no findings"; see the module
          docstring on why a conformant document can still carry warnings.
        * `error_count`, `warning_count` (int) — exact totals, counted before `MAX_FINDINGS`
          trims the list below them.
        * `findings` (list) — at most `MAX_FINDINGS` entries, each
          `{"code", "severity", "line", "message"}`. `line` is 1-based, and `None` for a
          finding about the document as a whole rather than about one line.
        * `findings_truncated` (bool) — whether `MAX_FINDINGS` bound.
        * `structure` (dict) — what the document HAS, independent of whether that is a
          finding: `h1` (str | None), `has_summary`, `section_count`, `link_count`,
          `has_optional_section`.
        * `version` (int) — `VALIDATION_VERSION`.

        An empty or whitespace-only document returns one `missing_h1` error rather than an
        empty findings list, so "nothing was checked" and "everything passed" can never be
        read as the same result.
    """
    findings, structure = _scan(text)

    error_count = sum(1 for finding in findings if finding["severity"] == ERROR)
    return {
        "conforms": error_count == 0,
        "error_count": error_count,
        "warning_count": len(findings) - error_count,
        "findings": findings[:MAX_FINDINGS],
        "findings_truncated": len(findings) > MAX_FINDINGS,
        "structure": structure,
        "version": VALIDATION_VERSION,
    }


# ---------------------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------------------
#
# One forward pass, because the spec's component list is ordered (module docstring). `phase`
# is how that order is enforced: it only ever increases, and a construct that belongs to an
# earlier phase than the one already reached is out of order by definition. The alternative —
# five independent searches — can tell you a blockquote exists but not that it came too late,
# which is the only thing worth knowing about its position.

_PHASE_START: Final = 0
"""Before the H1. Only a BOM and blank lines may appear here."""

_PHASE_SUMMARY: Final = 1
"""Directly after the H1, where the blockquote may still open."""

_PHASE_PROSE: Final = 2
"""After the summary closed: free-form markdown, headings excepted."""

_PHASE_SECTIONS: Final = 3
"""After the first H2. Every later H2 stays in this phase."""


def _scan(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Walk `text` once, collecting findings and the `structure` summary together.

    Both come out of the same pass on purpose: `section_count` and `link_count` are counted
    from the very lines the checks run against, so a document cannot report four links while
    the checker examined three.
    """
    findings: list[dict[str, Any]] = []
    h1: str | None = None
    has_summary = False
    section_count = 0
    link_count = 0
    has_optional_section = False

    phase = _PHASE_START
    in_fence = False
    in_section = False
    section_has_item = False
    section_line = 0

    for number, raw_line in enumerate(_lines(text), start=1):
        line = raw_line.rstrip()

        if _FENCE_PATTERN.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue

        heading = _HEADING_PATTERN.match(line)
        if heading:
            level = len(heading.group(1))
            # `group(2)` is `None` for a titleless heading — `#` on its own. Normalized to `""`
            # here so every check below reads a `str`, and so `_document_findings` sees the
            # empty title it needs to report `empty_h1` rather than a missing one.
            title = (heading.group(2) or "").strip()

            if level == 1:
                if h1 is None:
                    h1 = title
                    phase = max(phase, _PHASE_SUMMARY)
                else:
                    findings.append(
                        _finding(
                            "multiple_h1",
                            ERROR,
                            number,
                            "A second H1 appears here. The spec's H1 is the name of the "
                            "project or site, and a document has one title.",
                        )
                    )
                continue

            if h1 is None:
                findings.append(
                    _finding(
                        "content_before_h1",
                        ERROR,
                        number,
                        f"An H{level} appears before the document's H1. The spec orders its "
                        "components, and the H1 comes first.",
                    )
                )

            if level == 2:
                if in_section and not section_has_item:
                    findings.append(_empty_section(section_line))
                section_count += 1
                in_section = True
                section_has_item = False
                section_line = number
                phase = _PHASE_SECTIONS
                if title == _OPTIONAL_SECTION:
                    has_optional_section = True
                continue

            # H3 and deeper. The spec names exactly two heading levels: the H1 title and the
            # H2 section delimiters. Below the first H2 that makes an H3 undescribed rather
            # than forbidden, so it warns; above it, the prose section is specified as "any
            # type except headings", so the same line is an error. One check, two severities,
            # decided by where it sits.
            if phase == _PHASE_SECTIONS:
                findings.append(
                    _finding(
                        "heading_below_h2",
                        WARNING,
                        number,
                        f"An H{level} inside a file-list section. The spec describes sections "
                        "delimited by H2 and nothing deeper, so a consumer may not treat this "
                        "as a section boundary.",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "heading_in_prose",
                        ERROR,
                        number,
                        f"An H{level} appears before the first H2. The spec allows prose here "
                        '"of any type except headings".',
                    )
                )
            continue

        if line.lstrip().startswith(">"):
            if h1 is None:
                findings.append(
                    _finding(
                        "content_before_h1",
                        ERROR,
                        number,
                        "A blockquote appears before the document's H1. The spec orders its "
                        "components, and the H1 comes first.",
                    )
                )
            elif phase > _PHASE_SUMMARY:
                findings.append(
                    _finding(
                        "summary_out_of_order",
                        ERROR,
                        number,
                        "A blockquote appears after other content. The spec places the "
                        "summary blockquote directly after the H1.",
                    )
                )
            else:
                has_summary = True
            continue

        item = _LIST_ITEM_PATTERN.match(line)
        if item:
            if in_section:
                section_has_item = True
                link_count += _check_item(item.group(1), number, findings)
            # A list outside any H2 is prose, which the spec explicitly permits ("paragraphs,
            # lists, etc"). Not checked as a file list, and not a finding: only a list inside
            # an H2 section is making the claim that its items are links.
            continue

        if h1 is None:
            findings.append(
                _finding(
                    "content_before_h1",
                    ERROR,
                    number,
                    "Text appears before the document's H1. The spec orders its components, "
                    f"and the H1 comes first: {_excerpt(line)}",
                )
            )
            continue

        if phase == _PHASE_SECTIONS:
            findings.append(
                _finding(
                    "prose_in_section",
                    WARNING,
                    number,
                    "Prose inside a file-list section. The spec describes an H2 section as "
                    f"holding a file list, so this line may be ignored: {_excerpt(line)}",
                )
            )
        else:
            phase = _PHASE_PROSE

    if in_section and not section_has_item:
        findings.append(_empty_section(section_line))

    findings.extend(_document_findings(h1, has_summary, section_count, link_count))
    structure = {
        "h1": h1,
        "has_summary": has_summary,
        "section_count": section_count,
        "link_count": link_count,
        "has_optional_section": has_optional_section,
    }
    return findings, structure


def _check_item(content: str, number: int, findings: list[dict[str, Any]]) -> int:
    """Check one file-list item, appending findings, and return how many links it contributed.

    Returns `1` for an item carrying a usable link and `0` otherwise, so `link_count` counts
    what a consumer could actually follow rather than how many bullets were written. An item
    with an empty target is a bullet, not a link, and inflating the count with it would make
    `structure.link_count` disagree with the findings sitting beside it.
    """
    link = _LINK_PATTERN.match(content)
    if not link:
        findings.append(
            _finding(
                "item_without_link",
                ERROR,
                number,
                "A file-list item with no markdown link. The spec requires each item to "
                f"contain `[name](url)`: {_excerpt(content)}",
            )
        )
        return 0

    name, url, trailing = link.group(1), link.group(2).strip(), link.group(3)
    usable = True

    if not name.strip():
        findings.append(
            _finding(
                "empty_link_name",
                ERROR,
                number,
                "A file-list item's link has an empty name. The spec's item format is "
                "`[name](url)`, and a nameless link tells a consumer nothing about what it "
                "points at.",
            )
        )
    if not url:
        findings.append(
            _finding(
                "empty_link_url",
                ERROR,
                number,
                "A file-list item's link has an empty URL, so there is nothing to follow.",
            )
        )
        usable = False
    elif not _is_absolute(url):
        findings.append(
            _finding(
                "relative_link_url",
                WARNING,
                number,
                "A file-list item's link is relative. The spec calls these URLs where "
                "further detail is available, and a consumer that fetched this file by URL "
                f"has no base to resolve it against: {_excerpt(url)}",
            )
        )

    # "then optionally a `:` and notes about the file" — so text may follow the link, but only
    # introduced by a colon. Anything else is a suffix a spec-conformant parser reading only
    # the link and its notes would silently drop.
    if trailing.strip() and not trailing.startswith(":"):
        findings.append(
            _finding(
                "malformed_item_notes",
                ERROR,
                number,
                "Text follows a file-list item's link without a `:` introducing it. The "
                "spec's format is `[name](url)` then optionally `:` and notes: "
                f"{_excerpt(trailing.strip())}",
            )
        )
    elif trailing.startswith(":") and not trailing[1:].strip():
        findings.append(
            _finding(
                "empty_item_notes",
                WARNING,
                number,
                "A file-list item ends with a `:` and no notes after it. Either write the "
                "notes or drop the colon.",
            )
        )

    return 1 if usable else 0


def _document_findings(
    h1: str | None, has_summary: bool, section_count: int, link_count: int
) -> list[dict[str, Any]]:
    """The findings that are about the document as a whole, so they carry no line number.

    Collected after the pass rather than during it because each is a statement about what
    never appeared, and absence has no line to point at. `missing_h1` is the only `error` here
    — it is the spec's one requirement — and the rest are warnings about an artifact the spec
    permits and a consumer gets little from.
    """
    findings: list[dict[str, Any]] = []

    if h1 is None:
        findings.append(
            _finding(
                "missing_h1",
                ERROR,
                None,
                "The document has no H1. The spec calls an H1 with the name of the project "
                "or site the only required section.",
            )
        )
    elif not h1.strip():
        findings.append(
            _finding(
                "empty_h1",
                ERROR,
                None,
                "The document's H1 is empty. The spec's H1 is the name of the project or "
                "site, which an empty heading does not give.",
            )
        )

    if not has_summary:
        findings.append(
            _finding(
                "no_summary",
                WARNING,
                None,
                "No summary blockquote. The spec makes it optional, but it is where a "
                "consumer looks for the context needed to interpret the rest of the file.",
            )
        )

    if not section_count:
        findings.append(
            _finding(
                "no_sections",
                WARNING,
                None,
                "No H2 file-list sections. The spec permits this, and it leaves an artifact "
                "with no URLs for a consumer to follow.",
            )
        )
    elif not link_count:
        findings.append(
            _finding(
                "no_links",
                WARNING,
                None,
                "The file-list sections contain no usable links, so there is nothing for a "
                "consumer to follow.",
            )
        )

    return findings


def _empty_section(line: int) -> dict[str, Any]:
    """An H2 that closed without a single list item under it.

    A warning rather than an error: the spec's sections "contain file lists", but it does not
    say a section must be non-empty, and our own generator cannot produce one — `_scan` only
    opens a section on an H2, and `llms_txt.py` only writes an H2 when it has a bullet to put
    under it. This fires on third-party input and on a generator regression, which is exactly
    the pair worth a finding.
    """
    return _finding(
        "empty_section",
        WARNING,
        line,
        "This H2 section has no list items under it. A section with no file list gives a "
        "consumer nothing to follow.",
    )


def _finding(code: str, severity: str, line: int | None, message: str) -> dict[str, Any]:
    """One finding, in the shape `validate_llms_txt`'s `findings` list carries.

    `code` is the stable identifier — a caller grouping or filtering findings matches on it,
    never on `message`, which is prose and may be reworded without a `VALIDATION_VERSION`
    bump. `line` is 1-based to match what an editor shows.
    """
    return {"code": code, "severity": severity, "line": line, "message": message}


def _lines(text: str) -> list[str]:
    """`text` split into lines, with a leading BOM removed.

    `splitlines` rather than `split("\\n")` so a document with CRLF endings is not read as a
    single line ending in `\\r` — third-party input arrives with whatever endings its author's
    editor wrote, and a validator that reported one giant `content_before_h1` for a perfectly
    good CRLF file would be describing its own parser.
    """
    return text.removeprefix(_BOM).splitlines()


def _is_absolute(url: str) -> bool:
    """Whether `url` is absolute — a scheme and a host, which is what makes it followable by a
    consumer holding nothing but this file.

    `urlsplit` rather than a `startswith("http")` check: it is the same parser
    `llms_txt.py`'s `_origin` uses to decide what an origin is, so the two agree on what a
    URL's scheme and host are. Scheme-relative `//host/path` is deliberately not accepted —
    it resolves against the scheme of the document, and an `llms.txt` read from disk or from a
    model's context has no scheme to resolve against.
    """
    parts = urlsplit(url)
    return bool(parts.scheme and parts.netloc)


def _excerpt(text: str) -> str:
    """`text` cut to `MAX_MESSAGE_EXCERPT_CHARS`, for quoting an offending line in a message.

    Collapses whitespace first so a finding stays one readable line: the input may carry tabs
    or runs of spaces from the document being checked, and a message with a newline in it
    renders as two half-findings everywhere it is displayed.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_MESSAGE_EXCERPT_CHARS:
        return collapsed
    return collapsed[:MAX_MESSAGE_EXCERPT_CHARS].rstrip() + _EXCERPT_ELLIPSIS
