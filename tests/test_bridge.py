"""Tests for the cross-mode bridge / gateway engine.

Covers:
- BridgeEngine.targets() — matching, loop guard, address filter
- Router._handle_inbound dispatches bridge (mock transport receives re-injection)
- Interlock held → bridge skipped
- BridgeEngine.from_config() parses [[bridge]] TOML correctly
- /bridge TUI command
- radioapp bridge CLI command
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from radio_app.core.bridge import BridgeEngine, BridgeRule
from radio_app.core.filters import FilterAction, FilterEngine
from radio_app.core.groups import GroupRegistry
from radio_app.core.message import AddressType, UnifiedMessage
from radio_app.core.router import Router
from radio_app.core.store import MessageStore

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(
    transport: str = "js8call",
    address_type: AddressType = AddressType.BROADCAST,
    bridge_path: list[str] | None = None,
) -> UnifiedMessage:
    meta: dict = {}
    if bridge_path is not None:
        meta["bridge_path"] = bridge_path
    return UnifiedMessage(
        sender="W1TEST",
        content="hello",
        address_type=address_type,
        transport=transport,
        metadata=meta,
    )


def _engine(*rules: BridgeRule) -> BridgeEngine:
    return BridgeEngine(list(rules))


def _rule(frm: str, to: str, filt: str = "*") -> BridgeRule:
    return BridgeRule(from_transport=frm, to_transport=to, address_filter=filt)


# ---------------------------------------------------------------------------
# BridgeRule validation
# ---------------------------------------------------------------------------

def test_bridge_rule_invalid_filter():
    with pytest.raises(ValueError, match="address_filter"):
        BridgeRule(from_transport="a", to_transport="b", address_filter="invalid")


# ---------------------------------------------------------------------------
# BridgeEngine.targets() — basic matching
# ---------------------------------------------------------------------------

def test_targets_matching_rule():
    eng = _engine(_rule("js8call", "reticulum"))
    targets = eng.targets(_msg("js8call"))
    assert targets == ["reticulum"]


def test_targets_no_matching_rule():
    eng = _engine(_rule("js8call", "reticulum"))
    targets = eng.targets(_msg("meshcore"))
    assert targets == []


def test_targets_multiple_rules():
    eng = _engine(
        _rule("js8call", "reticulum"),
        _rule("js8call", "meshcore"),
    )
    targets = eng.targets(_msg("js8call"))
    assert set(targets) == {"reticulum", "meshcore"}


def test_targets_dedup_same_target():
    """Duplicate rules for the same target produce only one entry."""
    eng = _engine(
        _rule("js8call", "reticulum"),
        _rule("js8call", "reticulum"),
    )
    targets = eng.targets(_msg("js8call"))
    assert targets == ["reticulum"]


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------

def test_loop_guard_direct_cycle():
    """js8call → reticulum → js8call: second hop blocked by bridge_path."""
    eng = _engine(
        _rule("js8call", "reticulum"),
        _rule("reticulum", "js8call"),
    )
    # First hop: message arrives on js8call, path is empty.
    targets = eng.targets(_msg("js8call", bridge_path=[]))
    assert targets == ["reticulum"]

    # Second hop: message now on reticulum, path contains "js8call".
    targets = eng.targets(_msg("reticulum", bridge_path=["js8call"]))
    assert targets == []   # js8call is in path → blocked


def test_loop_guard_three_hop():
    """A→B→C→A chain: third hop to A is blocked."""
    eng = _engine(
        _rule("a", "b"),
        _rule("b", "c"),
        _rule("c", "a"),
    )
    # Hop C→A: path already contains "a".
    targets = eng.targets(_msg("c", bridge_path=["a", "b"]))
    assert targets == []


def test_loop_guard_unrelated_transport_not_blocked():
    """Only the transport already in bridge_path is excluded."""
    eng = _engine(
        _rule("js8call", "reticulum"),
        _rule("js8call", "meshcore"),
    )
    # Path contains "reticulum" — only that target is blocked.
    targets = eng.targets(_msg("js8call", bridge_path=["reticulum"]))
    assert targets == ["meshcore"]


# ---------------------------------------------------------------------------
# Address-type filter
# ---------------------------------------------------------------------------

def test_address_filter_broadcast_matches():
    eng = _engine(_rule("js8call", "reticulum", "broadcast"))
    assert eng.targets(_msg("js8call", AddressType.BROADCAST)) == ["reticulum"]


def test_address_filter_broadcast_rejects_direct():
    eng = _engine(_rule("js8call", "reticulum", "broadcast"))
    assert eng.targets(_msg("js8call", AddressType.DIRECT)) == []


def test_address_filter_group_matches():
    eng = _engine(_rule("js8call", "reticulum", "group"))
    assert eng.targets(_msg("js8call", AddressType.GROUP)) == ["reticulum"]


def test_address_filter_direct_matches():
    eng = _engine(_rule("js8call", "reticulum", "direct"))
    assert eng.targets(_msg("js8call", AddressType.DIRECT)) == ["reticulum"]


def test_address_filter_star_matches_all():
    eng = _engine(_rule("js8call", "reticulum", "*"))
    for at in AddressType:
        assert eng.targets(_msg("js8call", at)) == ["reticulum"]


# ---------------------------------------------------------------------------
# BridgeEngine.from_config()
# ---------------------------------------------------------------------------

def test_from_config_parses_rules():
    cfg = MagicMock()
    cfg.bridges = [
        {"from": "js8call", "to": "reticulum"},
        {"from": "reticulum", "to": "meshcore", "filter": "broadcast"},
    ]
    eng = BridgeEngine.from_config(cfg)
    assert len(eng.rules) == 2
    assert eng.rules[0].from_transport == "js8call"
    assert eng.rules[0].to_transport == "reticulum"
    assert eng.rules[0].address_filter == "*"
    assert eng.rules[1].address_filter == "broadcast"


def test_from_config_skips_invalid_rule(caplog):
    cfg = MagicMock()
    cfg.bridges = [
        {"from": "js8call"},   # missing "to"
        {"from": "js8call", "to": "reticulum"},
    ]
    import logging
    with caplog.at_level(logging.WARNING, logger="radio_app.core.bridge"):
        eng = BridgeEngine.from_config(cfg)
    assert len(eng.rules) == 1   # bad rule skipped, good rule kept


def test_from_config_empty():
    cfg = MagicMock()
    cfg.bridges = []
    eng = BridgeEngine.from_config(cfg)
    assert eng.rules == []


# ---------------------------------------------------------------------------
# Router integration: bridge dispatch
# ---------------------------------------------------------------------------

def _make_router(tmp_path, bridge: BridgeEngine, interlock=None):
    """Build a minimal Router with a real store, mock transport, and bridge."""
    db = str(tmp_path / "test.db")
    store = MessageStore(db)
    groups = GroupRegistry()
    filters = FilterEngine([], groups)

    def _caps(**kw):
        c = MagicMock()
        c.supports_groups = kw.get("supports_groups", False)
        c.supports_chunking = False
        c.max_message_size = 0
        c.requires_identity = False
        c.supports_encryption = False
        c.is_hf = False
        return c

    mock_t = MagicMock()
    mock_t.name = "js8call"
    mock_t.capabilities.return_value = _caps()
    mock_t.on_receive = MagicMock()
    mock_t.send = AsyncMock(return_value=True)

    mock_rns = MagicMock()
    mock_rns.name = "reticulum"
    mock_rns.capabilities.return_value = _caps()
    mock_rns.on_receive = MagicMock()
    mock_rns.send = AsyncMock(return_value=True)

    router = Router(
        transports=[mock_t, mock_rns],
        store=store,
        groups=groups,
        filters=filters,
        bridge=bridge,
        interlock=interlock,
    )
    return router, mock_t, mock_rns, store


def test_router_bridge_dispatches_to_target(tmp_path):
    """A message received on js8call is re-sent to reticulum per bridge rule."""
    bridge = _engine(_rule("js8call", "reticulum"))
    router, mock_js8, mock_rns, store = _make_router(tmp_path, bridge)

    msg = _msg("js8call")

    async def run():
        await router._handle_inbound(msg)
        # Let the bridge task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    mock_rns.send.assert_called_once()
    # The bridged message should have bridged=True metadata.
    sent_msg = mock_rns.send.call_args[0][0]
    assert sent_msg.metadata.get("bridged") is True
    assert sent_msg.metadata.get("bridge_origin") == "js8call"
    assert "js8call" in sent_msg.metadata.get("bridge_path", [])


def test_router_bridge_new_msg_id(tmp_path):
    """Bridged message gets a fresh msg_id (dedup safety)."""
    bridge = _engine(_rule("js8call", "reticulum"))
    router, _, mock_rns, _ = _make_router(tmp_path, bridge)
    msg = _msg("js8call")

    async def run():
        await router._handle_inbound(msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    sent_msg = mock_rns.send.call_args[0][0]
    assert sent_msg.msg_id != msg.msg_id


def test_router_bridge_skipped_when_interlock_busy(tmp_path):
    """Bridge dispatch is skipped when the target transport holds the interlock."""
    bridge = _engine(_rule("js8call", "reticulum"))

    interlock = MagicMock()
    interlock.blocked_by = MagicMock(return_value="reticulum")  # busy

    router, _, mock_rns, _ = _make_router(tmp_path, bridge, interlock=interlock)
    msg = _msg("js8call")

    async def run():
        await router._handle_inbound(msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    mock_rns.send.assert_not_called()


def test_router_bridge_not_called_for_drop(tmp_path):
    """DROPped messages are never bridged."""
    from radio_app.core.filters import FilterAction, FilterRule

    bridge = _engine(_rule("js8call", "reticulum"))
    db = str(tmp_path / "test.db")
    store = MessageStore(db)
    groups = GroupRegistry()

    drop_rule = MagicMock()
    drop_rule.matches = MagicMock(return_value=True)
    drop_rule.action = FilterAction.DROP
    filters = MagicMock()
    filters.decide = MagicMock(return_value=FilterAction.DROP)

    mock_js8 = MagicMock()
    mock_js8.name = "js8call"
    mock_js8.capabilities.return_value = MagicMock(supports_groups=False)
    mock_js8.on_receive = MagicMock()
    mock_rns = MagicMock()
    mock_rns.name = "reticulum"
    mock_rns.capabilities.return_value = MagicMock(supports_groups=False)
    mock_rns.on_receive = MagicMock()
    mock_rns.send = AsyncMock(return_value=True)

    router = Router(
        transports=[mock_js8, mock_rns],
        store=store,
        groups=groups,
        filters=filters,
        bridge=bridge,
    )

    msg = _msg("js8call")

    async def run():
        await router._handle_inbound(msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    mock_rns.send.assert_not_called()


# ---------------------------------------------------------------------------
# CLI: radioapp bridge
# ---------------------------------------------------------------------------

def test_cli_bridge_empty(tmp_path, capsys):
    """radioapp bridge with no rules prints a helpful message."""
    from radio_app.cli import main as cli_main

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[general]\n[logging]\nfile = \"\"\n[station]\ncallsign = \"W1T\"\n"
    )
    rc = cli_main(["--config", str(cfg), "bridge"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no bridge" in out.lower() or "[[bridge]]" in out


def test_cli_bridge_lists_rules(tmp_path, capsys):
    """radioapp bridge lists configured rules."""
    from radio_app.cli import main as cli_main

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[general]\n[logging]\nfile = \"\"\n[station]\ncallsign = \"W1T\"\n"
        "[[bridge]]\nfrom = \"js8call\"\nto = \"reticulum\"\n"
    )
    rc = cli_main(["--config", str(cfg), "bridge"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "js8call" in out
    assert "reticulum" in out


# ---------------------------------------------------------------------------
# TUI: /bridge command
# ---------------------------------------------------------------------------

pytest.importorskip("textual")
from textual.widgets import RichLog  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402

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


def test_tui_bridge_no_rules(tui_config):
    """/bridge with no rules shows a helpful hint."""
    async def run():
        app = RadioTUI(tui_config)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bridge")
            await pilot.pause()
            text = _log_text(app)
            assert "bridge" in text.lower()
            assert "[[bridge]]" in text or "config.toml" in text

    asyncio.run(run())


def test_tui_bridge_lists_rules(tmp_path):
    """/bridge with rules lists them."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        _TUI_CONFIG +
        "[[bridge]]\nfrom = \"js8call\"\nto = \"reticulum\"\n"
    )

    async def run():
        app = RadioTUI(str(cfg))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._select_mode("js8call")
            await pilot.pause()
            await app._handle_command("/bridge")
            await pilot.pause()
            text = _log_text(app)
            assert "js8call" in text
            assert "reticulum" in text

    asyncio.run(run())
