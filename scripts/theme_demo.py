"""Headless: theme/palette selection persists to config and restores on relaunch."""

import asyncio
import os

os.environ["RADIO_APP_CONFIG"] = "/tmp/radioapp_theme/config.toml"
os.makedirs("/tmp/radioapp_theme", exist_ok=True)

from radio_app.config import Config  # noqa: E402
from radio_app.ui.tui import RadioTUI  # noqa: E402


async def main():
    # 1) Launch, change the theme (as the command palette would), then exit.
    app = RadioTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        themes = list(app.available_themes)
        pick = next((t for t in themes if t != app.theme), themes[0])
        app.theme = pick
        await pilot.pause()
    saved = Config.load().ui.get("theme", "")
    print("picked theme   :", pick)
    print("saved to config:", saved, "->", "OK" if saved == pick else "MISMATCH")

    # 2) Relaunch a fresh app instance; it must restore the saved theme.
    app2 = RadioTUI()
    async with app2.run_test() as pilot:
        await pilot.pause()
        print("restored theme :", app2.theme, "->",
              "OK" if app2.theme == pick else "MISMATCH")


asyncio.run(main())

