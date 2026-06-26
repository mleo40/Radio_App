"""Tests for JS8Call group discovery / parsing (no sockets involved).

``query_groups`` itself is socket-bound, but all the parsing lives in the pure
``_groups_in_line`` helper, which is what we exercise here.
"""

from __future__ import annotations

import json

from radio_app.core.js8call_query import _groups_in_line


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def test_groups_in_line_explicit_string_field():
    line = _line({"type": "STATION.INFO", "params": {"GROUPS": "@TTP, @TTPNE"}})
    assert _groups_in_line(line) == ["@TTP", "@TTPNE"]


def test_groups_in_line_list_field():
    line = _line({"params": {"GROUPS": ["TTP", "EMCOMM"]}})
    assert _groups_in_line(line) == ["TTP", "EMCOMM"]


def test_groups_in_line_scrapes_freetext_info():
    line = _line({"params": {"INFO": "qrv on @ttp and @ares today"}})
    assert _groups_in_line(line) == ["@TTP", "@ARES"]


def test_groups_in_line_ignores_non_json_and_empty():
    assert _groups_in_line(b"not json\n") == []
    assert _groups_in_line(b"   \n") == []


def test_groups_in_line_no_groups_returns_empty():
    line = _line({"type": "STATION.CALLSIGN", "value": "N0CALL"})
    assert _groups_in_line(line) == []

