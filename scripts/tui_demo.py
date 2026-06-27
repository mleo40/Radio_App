"""Headless demo: drive the TUI like a user would and print the screen.

Not part of the package - a throwaway script to show interaction.
"""

import asyncio
import os

os.environ["RADIO_APP_CONFIG"] = "/tmp/radioapp_demo/config.toml"

from radio_app.core.message import UnifiedMessage  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402


async def main():
    app = RadioTUI()
    async with app.run_test(size=(96, 26)) as pilot:
        await pilot.pause()

        # 1) User starts a group conversation: types "/to @EMS" + Enter
        await pilot.click("#composer")
        for ch in "/to @EMS":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()

        # 2) User sends a message to the group
        for ch in "Net starts at 1900 local":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await pilot.pause()

        # 3) A message arrives over the air (simulate an inbound from a peer)
        inbound = UnifiedMessage.to_group("KE7XYZ", "EMS", "Copy, QRV on 40m")
        inbound.transport = "js8call"
        inbound.metadata = {"snr": -3, "freq": 7078000}
        await app.core.router._handle_inbound(inbound)
        await pilot.pause()

        # Capture what the user sees in the message pane + status bar.
        messages = app.query_one("#messages")

        print("\n========== CONVERSATIONS (left pane) ==========")
        for key in app._thread_keys:
            marker = " <-- selected" if key == app.current_target else ""
            print("  -", key + marker)

        print("\n========== MESSAGES (right pane) ==========")
        for line in messages.lines:
            text = "".join(seg.text for seg in line._segments)
            if text.strip():
                print(" ", text.rstrip())

        print("\n========== STATUS BAR ==========")
        up = ", ".join(app.core.running_transports) or "none up"
        print(f"  target: {app.current_target}    mode: {app.mode.value}"
              f"    transports: {up}")

        # Save a rendered SVG screenshot of the actual TUI.
        os.makedirs("docs", exist_ok=True)
        app.save_screenshot("docs/tui_demo.svg")
        print("\nSaved rendered screenshot to docs/tui_demo.svg")




asyncio.run(main())

