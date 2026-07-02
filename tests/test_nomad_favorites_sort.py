"""Favorited NomadNet nodes should sort first among currently-live nodes too.

Offline saved bookmarks already listed first unconditionally; this covers the
live-nodes section, which previously left favorites in raw announce order
(only starred, not sorted).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("textual")

from radio_app.ui.tui import RadioTUI  # noqa: E402

UTC = timezone.utc

CONFIG = """\
[general]
display_name = "Tester"
[logging]
file = ""
[transports.reticulum]
enabled = true
"""


@pytest.fixture()
def config_path(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG)
    return str(p)


def _node(dest: str, name: str, minutes_ago: int) -> dict:
    return {
        "dest": dest,
        "name": name,
        "last_seen": datetime.now(UTC) - timedelta(minutes=minutes_ago),
    }


def test_live_favorite_sorted_before_non_favorites(config_path, monkeypatch):
    """A favorite heard less recently than others still lists first (starred)."""
    fav_dest = "a" * 32
    other1_dest = "b" * 32
    other2_dest = "c" * 32

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ret = next(t for t in app.core.transports if t.name == "reticulum")
            monkeypatch.setattr(ret, "_running", True, raising=False)
            monkeypatch.setattr(type(ret), "running", property(lambda self: True))
            monkeypatch.setattr(
                ret,
                "known_nodes",
                lambda: [
                    _node(other1_dest, "Other1", 1),   # most recently heard
                    _node(other2_dest, "Other2", 2),
                    _node(fav_dest, "Favorite", 10),   # heard longest ago
                ],
            )
            app.core.favorites.add(fav_dest, label="Favorite", kind="node")

            app._show_nomadnet()
            await pilot.pause()

            dests = [n.get("dest") for n in app._nomad_nodes if n]
            assert dests == [fav_dest, other1_dest, other2_dest]

    asyncio.run(run())


def test_live_non_favorites_keep_relative_order(config_path, monkeypatch):
    """No favorites at all -> original (most-recent-first) order is untouched."""
    d1, d2, d3 = "a" * 32, "b" * 32, "c" * 32

    async def run():
        app = RadioTUI(config_path)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            ret = next(t for t in app.core.transports if t.name == "reticulum")
            monkeypatch.setattr(type(ret), "running", property(lambda self: True))
            monkeypatch.setattr(
                ret,
                "known_nodes",
                lambda: [_node(d1, "N1", 1), _node(d2, "N2", 2), _node(d3, "N3", 3)],
            )

            app._show_nomadnet()
            await pilot.pause()

            dests = [n.get("dest") for n in app._nomad_nodes if n]
            assert dests == [d1, d2, d3]

    asyncio.run(run())
