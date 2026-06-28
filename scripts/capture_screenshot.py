"""Capture a TUI screenshot with mock data for the README/docs.

Usage:
    python scripts/capture_screenshot.py [output.svg]

Defaults to docs/tui_screenshot.svg.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure src is on path when run from repo root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: F401 — ensure textual is importable

CONFIG = """\
[station]
callsign = "KC1QKM"
grid_square = "FN42"

[logging]
file = ""

[transports.js8call]
enabled = true
host = "127.0.0.1"
port = 2442
callsign = "KC1QKM"

[transports.meshcore]
enabled = true
connection = "tcp"
host = "127.0.0.1"
tcp_port = 5000

[transports.winlink]
enabled = true
pat_url = "http://127.0.0.1:8080"
callsign = "KC1QKM"
connect = "auto"

[transports.reticulum]
enabled = true
"""

MOCK_MESSAGES = [
    # (sender, content, transport, is_group)
    ("W1AW",   "Net starting in 5 minutes, all check in",    "js8call",  False),
    ("KE0XYZ", "Checking in from FN31 — 73!",                "js8call",  False),
    ("KD9ABC", "Good signal today, 20m is open",              "js8call",  False),
    ("@EMS",   "Traffic: welfare msg for Boston shelter ops", "js8call",  True),
    ("W1AW",   "Roger, relay received — will forward",        "js8call",  False),
]


async def run(out_path: Path) -> None:
    import tempfile

    from radio_app.core.message import AddressType, UnifiedMessage
    from radio_app.ui.tui import RadioTUI

    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(CONFIG)
        cfg_path = f.name

    app = RadioTUI(cfg_path)
    async with app.run_test(size=(132, 38)) as pilot:
        await pilot.pause()

        # Select JS8Call mode so the JS8 action bars are visible
        app._select_mode("js8call")
        await pilot.pause()

        # Inject mock messages so the conversation list and pane have content
        for sender, content, transport, is_group in MOCK_MESSAGES:
            msg = UnifiedMessage(
                sender=sender,
                content=content,
                transport=transport,
                address_type=AddressType.GROUP if is_group else AddressType.DIRECT,
                recipient="KC1QKM" if not is_group else "@EMS",
            )
            await app.core.router._handle_inbound(msg)
        app._refresh_threads()
        await pilot.pause()

        # Open the W1AW conversation so messages show in the right pane
        if "W1AW" in app._thread_keys:
            idx = app._thread_keys.index("W1AW")
            lv = app.query_one("#threads")
            lv.index = idx
            app._open_thread("W1AW", "js8call")
            await pilot.pause()

        # Capture
        svg = app.export_screenshot(title="Radio_App — JS8Call mode")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(svg)
    print(f"Screenshot saved to {out_path}")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/tui_screenshot.svg")
    asyncio.run(run(out))
