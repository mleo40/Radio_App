"""Unit tests for the read-only micron renderer and address parsing."""

from radio_app.core.micron import (
    DEFAULT_PAGE,
    MicronLink,
    make_link,
    parse_address,
    render_micron,
)
from radio_app.core.nomadnet import _match_prefix

# -- inline formatting --------------------------------------------------------


def test_plain_text_passthrough():
    page = render_micron("hello world")
    assert page.plain == "hello world"
    assert page.links == []


def test_bold_italic_underline_markup():
    page = render_micron("a `!bold`! b `*it`* c `_un`_ d")
    # Plain strips control codes.
    assert page.plain == "a bold b it c un d"
    assert "[b]bold[/]" in page.markup
    assert "[i]it[/]" in page.markup
    assert "[u]un[/]" in page.markup


def test_reset_clears_styles():
    page = render_micron("`!bold`` plain")
    assert page.plain == "bold plain"
    assert "[b]bold[/]" in page.markup
    # 'plain' must not be inside a bold span.
    assert "plain" in page.markup
    assert "[b]bold plain" not in page.markup


def test_foreground_colour_expands_to_hex():
    page = render_micron("`Ff00red`f done")
    assert "[#ff0000]red[/]" in page.markup
    assert page.plain == "red done"


def test_escaped_backtick_is_literal():
    page = render_micron("a \\` b")
    assert page.plain == "a ` b"


# -- headings, comments, dividers --------------------------------------------


def test_heading_is_bold_gold():
    page = render_micron(">Title")
    assert "Title" in page.markup
    assert page.plain.strip() == "Title"
    assert "[b #ffd700]" in page.markup


def test_comment_lines_are_skipped():
    page = render_micron("#comment\nvisible")
    assert "comment" not in page.plain
    assert "visible" in page.plain


def test_divider_line():
    page = render_micron("----")
    assert "\u2500" in page.plain


# -- links --------------------------------------------------------------------


def test_link_is_numbered_and_collected():
    page = render_micron("see `[the page`abc123:/page/x.mu]` now")
    assert len(page.links) == 1
    link = page.links[0]
    assert link.dest == "abc123"
    assert link.path == "/page/x.mu"
    assert link.label == "the page"
    assert "the page" in page.plain
    assert "[1]" in page.plain


def test_link_with_request_fields_dynamic():
    link = make_link("Buy`abc:/page/shop.mu`item=42|qty=2", base_dest=None)
    assert link.dest == "abc"
    assert link.path == "/page/shop.mu"
    assert link.fields == {"item": "42", "qty": "2"}


def test_link_relative_uses_base_dest():
    link = make_link("Home`:/page/index.mu", base_dest="deadbeef")
    assert link.dest == "deadbeef"
    assert link.path == "/page/index.mu"


def test_link_bare_name_becomes_page_path():
    link = make_link("About`about.mu", base_dest="deadbeef")
    assert link.path == "/page/about.mu"
    assert link.dest == "deadbeef"


def test_input_fields_are_skipped():
    # `<name`default>` is an input widget; read-only renderer drops it.
    page = render_micron("name: `<callsign`N0CALL> end")
    assert "callsign" not in page.plain
    assert "N0CALL" not in page.plain
    assert "name:" in page.plain
    assert "end" in page.plain


# -- address parsing ----------------------------------------------------------


def test_parse_address_full():
    dest, path, fields = parse_address("abc123:/page/index.mu")
    assert dest == "abc123"
    assert path == "/page/index.mu"
    assert fields == {}


def test_parse_address_hash_only_defaults_to_index():
    dest, path, _ = parse_address("abc123")
    assert dest == "abc123"
    assert path == DEFAULT_PAGE


def test_parse_address_with_fields():
    dest, path, fields = parse_address("abc:/page/d.mu|a=1|b=two")
    assert dest == "abc"
    assert path == "/page/d.mu"
    assert fields == {"a": "1", "b": "two"}


def test_parse_address_relative_uses_base():
    dest, path, _ = parse_address(":/page/x.mu", base_dest="base1")
    assert dest == "base1"
    assert path == "/page/x.mu"


# -- node hash prefix resolution ---------------------------------------------

_FULL_A = "51b9776931a0aabbccddeeff00112233"
_FULL_B = "51b9776931a0ffffffffffffffffffff"
_FULL_C = "c3dc75b40618112233445566778899aa"


def test_match_prefix_full_hash_passthrough():
    full, err = _match_prefix(_FULL_A, set())
    assert full == _FULL_A
    assert err is None


def test_match_prefix_unique_short_prefix():
    full, err = _match_prefix("c3dc75b40618", {_FULL_A, _FULL_C})
    assert full == _FULL_C
    assert err is None


def test_match_prefix_ambiguous_is_error():
    full, err = _match_prefix("51b9776931a0", {_FULL_A, _FULL_B})
    assert full is None
    assert err is not None
    assert "ambiguous" in err


def test_match_prefix_not_found_is_none_none():
    # No match but valid hex -> (None, None) so callers can keep waiting.
    full, err = _match_prefix("deadbeef", {_FULL_A})
    assert full is None
    assert err is None


def test_match_prefix_rejects_non_hex():
    full, err = _match_prefix("xyz123", {_FULL_A})
    assert full is None
    assert err is not None
    assert "hex" in err


def test_match_prefix_rejects_too_long():
    full, err = _match_prefix("a" * 40, set())
    assert full is None
    assert err is not None


# -- link self-reference resolution ------------------------------------------
# These guard the "ambiguous prefix" fix: when a micron page links to its own
# node via a short hex alias, we must dial the current page's full hash rather
# than try to resolve the alias globally (where two announced nodes can share
# those leading hex digits and trigger an ambiguity error).


_BASE = "51b9776931a0aabbccddeeff00112233"


def test_resolve_dest_prefers_base_when_link_dest_is_prefix_of_base():
    link = MicronLink(path="/page/about.mu", dest="51b977")
    assert link.resolve_dest(_BASE) == _BASE


def test_resolve_dest_keeps_link_dest_when_unrelated_to_base():
    other = "deadbeef0011223344556677889900aa"
    link = MicronLink(path="/page/x.mu", dest=other)
    # Link points elsewhere - don't rewrite it to the base.
    assert link.resolve_dest(_BASE) == other


def test_resolve_dest_uses_base_when_link_has_no_dest():
    link = MicronLink(path="/page/x.mu", dest=None)
    assert link.resolve_dest(_BASE) == _BASE


def test_resolve_dest_returns_link_dest_when_no_base():
    link = MicronLink(path="/page/x.mu", dest="51b977")
    assert link.resolve_dest(None) == "51b977"


def test_resolve_dest_handles_case_insensitive_prefix():
    link = MicronLink(path="/page/x.mu", dest="51B977")
    assert link.resolve_dest(_BASE) == _BASE
