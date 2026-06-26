"""Tests for the ``radioapp winlink`` CLI command (no Pat required)."""

from __future__ import annotations

import json

from radio_app.cli import _parse_field_args, _winlink_form_fields, main


def _write_cfg(tmp_path, enabled=True):
    p = tmp_path / "config.toml"
    block = "enabled = true" if enabled else "enabled = false"
    p.write_text(
        "[transports.winlink]\n"
        f"{block}\n"
        'pat_url = "http://127.0.0.1:9"\n'
        'callsign = "N0CALL"\n'
        'connect = "auto"\n'
        "[logging]\n"
        'file = ""\n'
    )
    return str(p)


def test_winlink_status_reports_unreachable(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    rc = main(["--config", cfg, "winlink", "status"])
    out = capsys.readouterr().out
    assert rc == 1  # Pat not answering on port 9
    assert "pat reachable    : no" in out
    assert "N0CALL" in out
    assert "auto: telnet" in out
    # Credentials guidance is always shown.
    assert "never stores it" in out


def test_winlink_status_when_disabled(tmp_path, capsys):
    cfg = _write_cfg(tmp_path, enabled=False)
    rc = main(["--config", cfg, "winlink", "status"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not enabled" in out


def test_winlink_defaults_to_status(tmp_path, capsys):
    cfg = _write_cfg(tmp_path)
    rc = main(["--config", cfg, "winlink"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "pat url" in out


# -- form-field discovery + response parsing (pure, no Pat) ------------------


def test_form_fields_detects_var_ask_html_and_braces():
    text = '<Var City> and <ask Name> and name="zip" and {County}'
    assert _winlink_form_fields(text) == ["City", "Name", "zip", "County"]


def test_form_fields_dedupes_case_insensitive():
    assert _winlink_form_fields("<Var City> name='city'") == ["City"]


def test_form_fields_none_detected():
    assert _winlink_form_fields("plain text, no inputs") == []


def test_parse_field_args_merges_pairs():
    responses, err = _parse_field_args(["a=1", "b=two"], None)
    assert err is None
    assert responses == {"a": "1", "b": "two"}


def test_parse_field_args_rejects_missing_equals():
    responses, err = _parse_field_args(["bad"], None)
    assert responses == {} and err and "KEY=VALUE" in err


def test_parse_field_args_field_overrides_file(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"city": "Boston", "state": "MA"}))
    responses, err = _parse_field_args(["city=Cambridge"], str(f))
    assert err is None
    assert responses == {"city": "Cambridge", "state": "MA"}


def test_parse_field_args_bad_json_file(tmp_path):
    f = tmp_path / "r.json"
    f.write_text("not json")
    responses, err = _parse_field_args(None, str(f))
    assert responses == {} and err and "--responses" in err



