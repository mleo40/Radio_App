"""Headless: active mode stays fixed while the Monitor streams all transports."""

import asyncio
import os

os.environ["RADIO_APP_CONFIG"] = "/tmp/radioapp_monitor/config.toml"
os.makedirs("/tmp/radioapp_monitor", exist_ok=True)

from radio_app.core.message import UnifiedMessage  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402


async def main():
    app = RadioTUI()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Force an active mode without the modal (simulate the F3 choice).
        app.active_transport = "reticulum"
        app._apply_mode()
        await pilot.pause()

        # Inbound on TWO transports - both must appear in the Monitor,
        # regardless of the active mode.
        m1 = UnifiedMessage(
            sender="abc123def456",
            content="ping over LoRa",
            transport="reticulum",
            recipient="me",
            metadata={"encrypted": True},
        )
        m2 = UnifiedMessage.to_group("KE7XYZ", "EMS", "net in 5")
        m2.transport = "js8call"
        await app.core.router._handle_inbound(m1)
        await app.core.router._handle_inbound(m2)
        await pilot.pause()

        print("active mode      :", app.active_transport)
        print("active view conv :", app.current_target)
        print("monitor (ALL transports):")
        for tk, tp in app._monitor_entries:
            print(f"   [{tp:<10}] {tk}")

        # Select the JS8 item in the monitor -> opens it and switches mode.
        app._open_from_monitor(1)
        await pilot.pause()
        print("\nafter selecting the JS8 monitor item:")
        print("active mode      :", app.active_transport)
        print("active view conv :", app.current_target)
        print("view             :", app.view)


asyncio.run(main())

