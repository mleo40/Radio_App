"""Headless check: first-run setup modal appears and saves station to config."""

import asyncio
import os

os.environ["RADIO_APP_CONFIG"] = "/tmp/radioapp_tui_setup/config.toml"
os.makedirs("/tmp/radioapp_tui_setup", exist_ok=True)

from radio_app.config import Config  # noqa: E402
from radio_app.ui.tui import RadioTUI, SetupScreen  # noqa: E402


async def main():
    # Fresh config with no callsign -> setup modal should appear.
    app = RadioTUI()
    async with app.run_test(size=(96, 28)) as pilot:
        for _ in range(6):
            await pilot.pause()
            if isinstance(app.screen, SetupScreen):
                break
        screen = app.screen
        print("Top screen on first run:", type(screen).__name__)
        assert isinstance(screen, SetupScreen), "expected SetupScreen on first run"

        # User fills the form and clicks Save (query within the modal screen).
        screen.query_one("#s-call").value = "W1AW"
        screen.query_one("#s-grid").value = "FN31pr"
        await pilot.click("#s-save")
        await pilot.pause()
        await pilot.pause()

        print("Station after save:", app.core.station.callsign,
              app.core.station.grid_square)

    # Confirm it persisted to the TOML file.
    cfg = Config.load()
    print("Persisted to config:", cfg.station)


asyncio.run(main())

