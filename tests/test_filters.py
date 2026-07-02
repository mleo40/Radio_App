"""Tests for inbound filter rule management.

Covers:
- FilterRule.to_config() round-trip
- build_filter_rule() validation (action, match keys)
- FilterEngine add/edit/remove/move_rule + find_index
- FilterEngine.save() writes back to config
- radioapp filters CLI command
- /filters TUI command
"""
from __future__ import annotations

import pytest

from radio_app.config import Config
from radio_app.core.filters import (
    FilterAction,
    FilterEngine,
    FilterRule,
    build_filter_rule,
)
from radio_app.core.groups import GroupRegistry


def _engine(*rules: FilterRule) -> FilterEngine:
    return FilterEngine(list(rules), GroupRegistry())


# ---------------------------------------------------------------------------
# FilterRule.to_config()
# ---------------------------------------------------------------------------

def test_to_config_with_match():
    r = FilterRule(name="ems-hf", action=FilterAction.NOTIFY, match={"group": "EMS"})
    assert r.to_config() == {
        "name": "ems-hf", "action": "notify", "match": {"group": "EMS"}
    }


def test_to_config_without_match():
    r = FilterRule(name="Default", action=FilterAction.SHOW)
    assert r.to_config() == {"name": "Default", "action": "show"}


# ---------------------------------------------------------------------------
# build_filter_rule()
# ---------------------------------------------------------------------------

def test_build_filter_rule_basic():
    r = build_filter_rule("ems-hf", "notify", ["group=EMS", "transport=js8call"])
    assert r.name == "ems-hf"
    assert r.action is FilterAction.NOTIFY
    assert r.match == {"group": "EMS", "transport": "js8call"}


def test_build_filter_rule_no_match_args():
    r = build_filter_rule("Default", "show", [])
    assert r.match == {}


def test_build_filter_rule_invalid_action():
    with pytest.raises(ValueError, match="invalid action"):
        build_filter_rule("x", "bogus", [])


def test_build_filter_rule_bad_token():
    with pytest.raises(ValueError, match="key=value"):
        build_filter_rule("x", "show", ["not-a-pair"])


def test_build_filter_rule_unknown_key():
    with pytest.raises(ValueError, match="unknown match key"):
        build_filter_rule("x", "show", ["bogus=1"])


# ---------------------------------------------------------------------------
# FilterEngine.find_index()
# ---------------------------------------------------------------------------

def test_find_index_by_name():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    assert eng.find_index("b") == 1


def test_find_index_by_number():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    assert eng.find_index("2") == 1


def test_find_index_missing():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW))
    assert eng.find_index("nope") is None
    assert eng.find_index("99") is None


# ---------------------------------------------------------------------------
# FilterEngine.add_rule()
# ---------------------------------------------------------------------------

def test_add_rule_appends_when_no_catchall():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW, match={"group": "X"}))
    eng.add_rule(FilterRule(name="b", action=FilterAction.NOTIFY, match={"group": "Y"}))
    assert [r.name for r in eng.rules] == ["a", "b"]


def test_add_rule_inserts_before_catchall():
    eng = _engine(
        FilterRule(
            name="specific", action=FilterAction.NOTIFY, match={"group": "EMS"}
        ),
        FilterRule(name="Default", action=FilterAction.SHOW),
    )
    eng.add_rule(
        FilterRule(name="new", action=FilterAction.FILE, match={"group": "OPS"})
    )
    assert [r.name for r in eng.rules] == ["specific", "new", "Default"]


def test_add_rule_duplicate_name_rejected():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW))
    with pytest.raises(ValueError, match="already exists"):
        eng.add_rule(FilterRule(name="a", action=FilterAction.DROP))


# ---------------------------------------------------------------------------
# FilterEngine.edit_rule()
# ---------------------------------------------------------------------------

def test_edit_rule_replaces_action_and_match():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW, match={"group": "X"}))
    assert eng.edit_rule("a", "drop", ["group=Y"])
    r = eng.rules[0]
    assert r.name == "a"
    assert r.action is FilterAction.DROP
    assert r.match == {"group": "Y"}


def test_edit_rule_missing_returns_false():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW))
    assert eng.edit_rule("nope", "drop", []) is False


def test_edit_rule_invalid_action_raises():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW))
    with pytest.raises(ValueError):
        eng.edit_rule("a", "bogus", [])


# ---------------------------------------------------------------------------
# FilterEngine.remove_rule()
# ---------------------------------------------------------------------------

def test_remove_rule_by_name():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    removed = eng.remove_rule("a")
    assert removed is not None and removed.name == "a"
    assert [r.name for r in eng.rules] == ["b"]


def test_remove_rule_missing():
    eng = _engine(FilterRule(name="a", action=FilterAction.SHOW))
    assert eng.remove_rule("nope") is None


# ---------------------------------------------------------------------------
# FilterEngine.move_rule()
# ---------------------------------------------------------------------------

def test_move_rule_up():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    assert eng.move_rule("b", "up")
    assert [r.name for r in eng.rules] == ["b", "a"]


def test_move_rule_down():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    assert eng.move_rule("a", "down")
    assert [r.name for r in eng.rules] == ["b", "a"]


def test_move_rule_at_boundary_noop():
    eng = _engine(
        FilterRule(name="a", action=FilterAction.SHOW),
        FilterRule(name="b", action=FilterAction.FILE),
    )
    assert eng.move_rule("a", "up") is False
    assert eng.move_rule("b", "down") is False


# ---------------------------------------------------------------------------
# FilterEngine.save()
# ---------------------------------------------------------------------------

def test_save_writes_config(tmp_path):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[station]\ncallsign = \"W1T\"\n")
    cfg = Config.load(cfg_path)
    eng = FilterEngine.from_config(cfg, GroupRegistry())
    eng.add_rule(
        FilterRule(name="ems-hf", action=FilterAction.NOTIFY, match={"group": "EMS"})
    )
    eng.save(cfg)

    reloaded = Config.load(cfg_path)
    assert reloaded.filters == [
        {"name": "ems-hf", "action": "notify", "match": {"group": "EMS"}}
    ]


# ---------------------------------------------------------------------------
# CLI: radioapp filters
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path, extra=""):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[general]\n[logging]\nfile = \"\"\n[station]\ncallsign = \"W1T\"\n" + extra
    )
    return cfg


def test_cli_filters_empty(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)
    rc = cli_main(["--config", str(cfg), "filters"])
    assert rc == 0
    assert "no filter rules" in capsys.readouterr().out.lower()


def test_cli_filters_add_list_del(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)

    rc = cli_main(
        ["--config", str(cfg), "filters", "add", "ems-hf", "notify", "group=EMS"]
    )
    assert rc == 0
    assert "added rule 'ems-hf'" in capsys.readouterr().out

    rc = cli_main(["--config", str(cfg), "filters", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ems-hf" in out and "notify" in out and "group=EMS" in out

    rc = cli_main(["--config", str(cfg), "filters", "del", "ems-hf"])
    assert rc == 0
    assert "deleted rule 'ems-hf'" in capsys.readouterr().out

    rc = cli_main(["--config", str(cfg), "filters", "list"])
    assert "no filter rules" in capsys.readouterr().out.lower()


def test_cli_filters_edit(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(
        tmp_path,
        '[[filters]]\nname = "a"\naction = "show"\n[filters.match]\ngroup = "X"\n',
    )
    rc = cli_main(["--config", str(cfg), "filters", "edit", "a", "drop", "group=Y"])
    assert rc == 0
    assert "updated rule 'a'" in capsys.readouterr().out

    reloaded = Config.load(cfg)
    assert reloaded.filters == [
        {"name": "a", "action": "drop", "match": {"group": "Y"}}
    ]


def test_cli_filters_mv(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(
        tmp_path,
        '[[filters]]\nname = "a"\naction = "show"\n'
        '[[filters]]\nname = "b"\naction = "file"\n',
    )
    rc = cli_main(["--config", str(cfg), "filters", "mv", "b", "up"])
    assert rc == 0
    assert "moved 'b' up" in capsys.readouterr().out

    reloaded = Config.load(cfg)
    assert [r["name"] for r in reloaded.filters] == ["b", "a"]


def test_cli_filters_add_invalid_action(tmp_path, capsys):
    from radio_app.cli import main as cli_main

    cfg = _write_cfg(tmp_path)
    rc = cli_main(["--config", str(cfg), "filters", "add", "x", "bogus"])
    assert rc == 2
    assert "invalid action" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# TUI: /filters command
# ---------------------------------------------------------------------------

pytest.importorskip("textual")
from textual.widgets import RichLog  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402
import asyncio  # noqa: E402

_TUI_CONFIG = """\
[general]
display_name = "T"
[logging]
file = ""
[station]
callsign = "W1TEST"
[transports.js8call]
enabled = true
port = 2442
"""


@pytest.fixture()
def tui_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(_TUI_CONFIG)
    return str(p)


def _log_text(app: RadioTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#messages", RichLog).lines)


def test_tui_filters_no_rules(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/filters")
            await pilot.pause()
            text = _log_text(app)
            assert "no filter rules" in text.lower()

    asyncio.run(run())


def test_tui_filters_add_list_del(tui_config):
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()

            await app._handle_command("/filters add ems-hf notify group=EMS")
            await pilot.pause()
            assert "added" in _log_text(app).lower()
            # Takes effect immediately on the live engine, no restart needed.
            assert [r.name for r in app.core.filters.rules] == ["ems-hf"]

            await app._handle_command("/filters")
            await pilot.pause()
            text = _log_text(app)
            assert "ems-hf" in text and "notify" in text

            await app._handle_command("/filters del ems-hf")
            await pilot.pause()
            assert "deleted" in _log_text(app).lower()
            assert app.core.filters.rules == []

    asyncio.run(run())
