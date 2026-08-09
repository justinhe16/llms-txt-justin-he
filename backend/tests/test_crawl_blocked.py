"""Tests for `app.features.crawl.internals.blocked` — `classify_block` and
`merge_block_reason`, the pure, no-I/O module that tells a genuine response apart from a
detected WAF/CDN access challenge or denial.

No database, no network, no `httpx.MockTransport`: `classify_block` takes a status code, a
plain header mapping, and a body string, so everything here is an in-memory call and an
assertion on its return value — the same category `tests/test_crawl_robots.py` and
`tests/test_url_ranking.py` are in for their own feature-owned pure modules.
"""

import functools
from pathlib import Path

from app.features.crawl.internals.blocked import classify_block, merge_block_reason


_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> str:
    """See `tests/test_crawl_extract.py`'s helper of the same name — identical contract."""
    return (_FIXTURES / name).read_text(encoding="utf-8")


CHALLENGE_BODY = _load_fixture("cloudflare_challenge.html")


# --- Rule 1: any 2xx is never a block, unconditionally, first -----------------------------


def test_a_2xx_status_is_never_a_block_even_with_challenge_looking_headers_and_body() -> None:
    """[Rule 1 / non-regression]. The structural guarantee this whole module rests on: a
    successful response is NEVER reclassified as a block, no matter what its headers or body
    contain. Every other signal a real challenge page would carry is present here on purpose —
    `cf-mitigated`, a Cloudflare `server` header, and the fixture's own challenge-shaped
    `<title>` — so this only passes if the status check runs, and wins, before any of them are
    even consulted."""
    headers = {"cf-mitigated": "challenge", "server": "cloudflare"}
    assert classify_block(200, headers, CHALLENGE_BODY) is None


def test_every_2xx_status_in_range_is_none() -> None:
    for status in (200, 201, 204, 226, 299):
        assert classify_block(status, {}, "") is None


# --- Rule 2: cf-mitigated header, any value, case-insensitive name ------------------------


def test_a_cf_mitigated_header_on_a_403_is_a_challenge() -> None:
    assert classify_block(403, {"cf-mitigated": "challenge"}, "") == "challenge"


def test_cf_mitigated_header_name_is_matched_case_insensitively() -> None:
    assert classify_block(403, {"CF-Mitigated": "challenge"}, "") == "challenge"
    assert classify_block(403, {"Cf-MITIGATED": "challenge"}, "") == "challenge"


def test_cf_mitigated_fires_on_any_value_not_only_challenge() -> None:
    """The header's mere PRESENCE is Cloudflare's own signal — this module does not require
    (or even read) any particular value."""
    assert classify_block(403, {"cf-mitigated": "some-other-token"}, "") == "challenge"
    assert classify_block(403, {"cf-mitigated": ""}, "") == "challenge"


def test_cf_mitigated_fires_ahead_of_the_title_scan_even_on_a_body_that_would_fail_rule_3() -> None:
    """[Rule ordering]. `cf-mitigated` is checked before rule 3's body-shaped requirements, so
    a response this header names a challenge is one regardless of whether its body would ever
    have passed the `server`/title checks on its own."""
    headers = {"cf-mitigated": "challenge", "server": "nginx"}
    assert classify_block(403, headers, "not a challenge page at all") == "challenge"


def test_cf_mitigated_on_a_2xx_still_yields_none() -> None:
    """Rule 1 runs first and unconditionally — this is the same non-regression guarantee as
    the top-of-file test, restated against the header rule specifically."""
    assert classify_block(200, {"cf-mitigated": "challenge"}, "") is None


# --- Rule 3: 403/503 + cloudflare server + bounded body + matching <title> ----------------


def test_a_cloudflare_challenge_shaped_403_is_a_challenge() -> None:
    headers = {"server": "cloudflare"}
    assert classify_block(403, headers, CHALLENGE_BODY) == "challenge"


def test_a_cloudflare_challenge_shaped_503_is_also_a_challenge() -> None:
    """Rule 3 fires on 403 OR 503 — a managed challenge is not always served with 403."""
    headers = {"server": "cloudflare"}
    assert classify_block(503, headers, CHALLENGE_BODY) == "challenge"


def test_server_header_match_is_case_insensitive_and_a_substring() -> None:
    headers = {"server": "Cloudflare"}
    assert classify_block(403, headers, CHALLENGE_BODY) == "challenge"


def test_a_403_missing_any_one_of_the_three_rule_3_conditions_is_not_a_challenge() -> None:
    """[Rule 3 / all three required]. Status + server + title all have to agree; any one
    missing falls through to rule 4's bare `401`/`403` -> `"denied"`, never `"challenge"`."""
    # Right status and title, wrong (non-Cloudflare) server.
    assert classify_block(403, {"server": "nginx"}, CHALLENGE_BODY) == "denied"
    # Right status and server, but a body with no matching title at all.
    assert (
        classify_block(403, {"server": "cloudflare"}, "<html><body>ordinary</body></html>")
        == "denied"
    )
    # Right server and title, but a status rule 3 does not cover.
    assert classify_block(404, {"server": "cloudflare"}, CHALLENGE_BODY) is None


def test_a_body_over_the_length_ceiling_is_never_title_scanned_for_rule_3() -> None:
    """A response this large failing every other rule is evidence it is not a challenge page —
    real challenge interstitials are a few kilobytes at most. Falls through to rule 4's bare
    `"denied"` instead, exactly as a body with no matching title does."""
    oversized = CHALLENGE_BODY + ("x" * 70_000)
    assert classify_block(403, {"server": "cloudflare"}, oversized) == "denied"


def test_the_title_scan_is_bounded_to_the_first_4096_characters() -> None:
    """A real `<title>` sits in `<head>`, at the very start of any document — padding a
    challenge-shaped body with enough leading filler to push its `<title>` past the scan
    window is what this test proves does NOT still classify as a challenge, without needing
    to construct a body anywhere near the `_MAX_BODY_CHARS` ceiling itself."""
    padded = "<!--" + ("x" * 5_000) + "-->" + CHALLENGE_BODY
    assert len(padded) <= 65_536
    assert classify_block(403, {"server": "cloudflare"}, padded) == "denied"


def test_title_matching_is_exact_not_substring() -> None:
    """A real page whose title happens to CONTAIN challenge-shaped words is not a challenge —
    the vocabulary is matched exactly, case- and whitespace-insensitively, never as a
    substring."""
    body = (
        "<html><head><title>A Moment of Attention: Debugging Race Conditions</title></head></html>"
    )
    assert classify_block(403, {"server": "cloudflare"}, body) == "denied"


def test_title_matching_is_case_and_whitespace_insensitive() -> None:
    body = "<html><head><title>  JUST A MOMENT...  </title></head></html>"
    assert classify_block(403, {"server": "cloudflare"}, body) == "challenge"


def test_every_known_challenge_title_matches() -> None:
    for title in ("Just a moment...", "Attention Required! | Cloudflare", "Checking your browser"):
        body = f"<html><head><title>{title}</title></head></html>"
        assert classify_block(403, {"server": "cloudflare"}, body) == "challenge", title


def test_a_missing_server_header_falls_through_rule_3_to_denied() -> None:
    assert classify_block(403, {}, CHALLENGE_BODY) == "denied"


# --- Rule 4: bare 401/403 -> denied ---------------------------------------------------------


def test_a_401_with_no_challenge_markers_is_denied() -> None:
    """401 is not one of rule 3's two statuses at all — it can only ever reach rule 4."""
    assert classify_block(401, {}, "") == "denied"
    assert classify_block(401, {"server": "cloudflare"}, CHALLENGE_BODY) == "denied"


def test_a_bare_403_with_no_challenge_markers_is_denied() -> None:
    assert classify_block(403, {"server": "nginx"}, "Forbidden") == "denied"


# --- Rule 5: everything else, 429 and a bare 503 included, is None -------------------------


def test_a_429_is_never_a_block() -> None:
    """A rate limit is not an access block — the site is reachable and answering, just asking
    this crawler to slow down."""
    assert classify_block(429, {}, "") is None
    assert classify_block(429, {"server": "cloudflare"}, CHALLENGE_BODY) is None


def test_a_bare_503_with_no_cloudflare_markers_is_never_a_block() -> None:
    """An ordinary 'temporarily unavailable,' identical to what an overloaded origin serves
    with no WAF in front of it at all."""
    assert classify_block(503, {}, "Service Unavailable") is None
    assert classify_block(503, {"server": "nginx"}, "Service Unavailable") is None


def test_every_other_4xx_and_5xx_status_is_none() -> None:
    for status in (400, 404, 405, 410, 429, 500, 502, 504):
        assert classify_block(status, {}, "") is None


# --- Never raises ----------------------------------------------------------------------------


def test_classify_block_never_raises_on_hostile_or_malformed_input() -> None:
    """The same 'never raises' contract every pure module in this feature holds
    (`internals/robots.py`, `internals/links.py`) — this runs on attacker-supplied bytes. A
    huge body exceeds `_MAX_BODY_CHARS` and is never even scanned for a title, so it falls
    through rule 3 to rule 4's bare `"denied"`, exactly as a body with no `<title>` at all
    does — neither call raises, which is the property this test actually pins."""
    huge_body = "<title>" + ("a" * 1_000_000) + "</title>"
    assert classify_block(403, {"server": "cloudflare"}, huge_body) == "denied"
    assert classify_block(403, {"server": "cloudflare"}, "<title") == "denied"
    assert classify_block(403, {"server": "cloudflare"}, "") == "denied"


# --- merge_block_reason: order-independent, challenge > denied > None ----------------------


def test_merge_block_reason_is_the_identity_starting_from_none() -> None:
    assert merge_block_reason(None, None) is None
    assert merge_block_reason(None, "denied") == "denied"
    assert merge_block_reason(None, "challenge") == "challenge"


def test_challenge_outranks_denied_regardless_of_argument_order() -> None:
    assert merge_block_reason("challenge", "denied") == "challenge"
    assert merge_block_reason("denied", "challenge") == "challenge"


def test_merging_the_same_reason_twice_is_a_no_op() -> None:
    assert merge_block_reason("denied", "denied") == "denied"
    assert merge_block_reason("challenge", "challenge") == "challenge"


def test_merge_block_reason_is_order_independent_across_a_fold() -> None:
    """The property that actually matters in production: folding the SAME set of reasons in
    two different orders (standing in for two different completion orders of concurrent
    frontier fetches) produces the same result either way."""
    reasons_a = [None, "denied", "challenge", "denied"]
    reasons_b = ["denied", "challenge", None, "denied"]

    result_a = functools.reduce(merge_block_reason, reasons_a, None)
    result_b = functools.reduce(merge_block_reason, reasons_b, None)

    assert result_a == result_b == "challenge"
