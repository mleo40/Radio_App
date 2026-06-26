"""Headless walkthrough of the mode-workspace shell (Milestone 1).

Demonstrates the "single pane of glass": a persistent mode selector with a
health dot per transport, switching between operating modes, the Watch surface
(all transports), and the Health surface (passive reachability probes).

No radio hardware needed - the transports report DOWN because nothing is
listening on their endpoints, which is exactly what the health dots show.
"""

import asyncio
import os

os.environ["RADIO_APP_CONFIG"] = "/tmp/radioapp_modeshell/config.toml"
os.makedirs("/tmp/radioapp_modeshell", exist_ok=True)
with open(os.environ["RADIO_APP_CONFIG"], "w") as fh:
    fh.write(
        "[general]\n"
        'display_name = "Tester"\n'
        "[logging]\n"
        'file = ""\n'
        "[transports.js8call]\nenabled = true\nport = 2442\n"
        "[transports.mercury]\nenabled = true\nport = 7373\n"
    )

from textual.widgets import Button  # noqa: E402

from radio_app.ui.tui import RadioTUI  # noqa: E402


async def main() -> None:
    app = RadioTUI()
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one("#modebar")
        print("mode selector:", [b.id for b in bar.query(Button)])

        # Passive reachability probe drives the health dots.
        app._refresh_health()
        await pilot.pause()
        await asyncio.sleep(0.3)
        await pilot.pause()
        print("health:", {k: v.value for k, v in app._health.items()})

        # Pick an operating mode (as a tap on its chip would).
        app._select_mode("js8call")
        await pilot.pause()
        print("active mode:", app.active_transport, "view:", app.view)

        # Utility surfaces.
        app._show_watch()
        await pilot.pause()
        print("watch  -> switcher:", app.query_one("#main").current)
        app.action_health()
        await pilot.pause()
        print("health -> switcher:", app.query_one("#main").current)


asyncio.run(main())

