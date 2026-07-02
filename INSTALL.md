# Installing Radio_App

## Quick install (Debian / Ubuntu)

Debian 12 (bookworm) and 13 (trixie) enforce **PEP 668** ("externally-managed-environment"):
a bare `pip install` outside a virtual environment fails. Use a venv — don't reach for
`--break-system-packages`.

```bash
# 1. System prerequisites
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip

# 2. Clone
git clone https://github.com/mleo40/Radio_App.git
cd Radio_App

# 3. Create and activate a venv (also puts `radioapp` on PATH automatically,
#    sidestepping the "command not found after pip install" issue below)
python3 -m venv .venv
source .venv/bin/activate

# 4. Install — pick extras based on what you'll actually use (see pyproject.toml):
pip install -e ".[all]"          # everything: reticulum + tui + meshcore + health
# or narrower, e.g.:
# pip install -e ".[tui]"        # just the Textual UI, no Reticulum/MeshCore
# pip install -e .               # core + CLI only, stdlib-only, no radio deps

# 5. One-time interactive setup (writes ~/.config/radio_app/config.toml)
radioapp setup

# 6. Run
radioapp tui        # Textual terminal UI
# or use CLI subcommands directly, e.g.:
radioapp status
```

`requires-python = ">=3.10"`. Debian 12 ships Python 3.11, Debian 13 ships 3.12 — both fine.

## External programs Radio_App expects (none bundled or installed by pip)

Radio_App is a pure client to all of these — it never installs, bundles, or (with one
optional exception) launches them; it only opens sockets/probes ports for whatever is already
running.

| External program | Used by | How Radio_App talks to it | Required? |
|---|---|---|---|
| **JS8Call** | JS8Call transport | TCP/JSON API, `127.0.0.1:2442` | only if the `js8call` transport is enabled |
| **WSJT-X** | WSJT-X transport | UDP datagrams it sends, `0.0.0.0:2237` | only if the `wsjt_x` transport is enabled |
| **Pat** ([la5nta/pat](https://github.com/la5nta/pat), Winlink client) | Winlink transport | HTTP API, `http://127.0.0.1:8080` | only if the `winlink` transport is enabled |
| **ARDOP modem** (e.g. `ardopcf`) | Winlink transport, `ardop` connect method | Pat connects to it directly; Radio_App only TCP-probes the port for Health status (`127.0.0.1:8515` by convention) | only if using Winlink's `ardop` connect method |
| **VARA HF / VARA FM modem** | Winlink transport, `varahf`/`varafm` connect methods | same — Pat-side; Radio_App just probes (`127.0.0.1:8300` by convention) | only if using `varahf`/`varafm` |
| **`rnsd`** (Reticulum daemon) | Reticulum transport | RNS shared-instance local socket | only if the `reticulum` transport is enabled. Installing the `reticulum` extra installs the `rnsd` binary into your venv, but **Radio_App never starts it itself** — run it as its own process |
| **gpsd** | Position + time-consensus | gpsd JSON streaming protocol, `127.0.0.1:2947` | optional — live GPS position, and the top-priority time source |
| **chrony** (`chronyc` binary) | Time-consensus | subprocess: `chronyc tracking` | optional — 2nd-priority time source; never started by Radio_App |
| **ntpd** (`ntpq` binary) | Time-consensus | subprocess: `ntpq -c rv` | optional — 3rd-priority time source fallback |
| **MeshCore device** (physical LoRa radio, not a desktop app) | MeshCore transport | USB-serial, TCP, or BLE to the device's own firmware | only if the `meshcore` transport is enabled |

**One partial exception:** Winlink's `modem_cmd` config key lets Radio_App optionally
auto-spawn an ARDOP/VARA modem subprocess itself if the port isn't already open — but only if
you explicitly set `modem_cmd` in config; there's no default command, so this is opt-in.

**Explicitly not needed:** the separate `nomadnet` program. NomadNet page browsing is
reimplemented in-app as a read-only micron renderer over RNS — the `reticulum` extra is
sufficient on its own.

**Never shells out to:** a web browser, `notify-send`, or any audio tool (`aplay`/`arecord`).

None of the external programs above are required just to try the app — `pip install -e .`
(no extras) plus `radioapp tui` gets you the core/CLI/TUI with no transports enabled at all.

## Troubleshooting

### `radioapp: command not found` after `pip install`

Only an issue without a venv (pip installs scripts to `~/.local/bin`, not on PATH by default).
Using a venv (step 3 above) sidesteps this entirely — an activated venv puts its own `bin/` on
PATH automatically. Without a venv, add it to your shell profile instead:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### `ImportError: cannot import name '_psutil_linux'`

Only relevant with the `health` extra. Caused by a pip/Python interpreter mismatch (pip
resolved a psutil build for a different Python than the one running). Using a venv from the
start avoids the mismatch in the first place. If it happens anyway:

```bash
sudo apt install python3.11-dev   # match your actual Python version
pip install --no-binary psutil "psutil>=5.9.1" --user
```

### pip resolves the wrong Python / wrong site-packages

Confirm pip and python3 agree on the same interpreter:

```bash
python3 --version
pip --version   # should show the same version and path
```

If they diverge, invoke pip through the interpreter explicitly: `python3 -m pip install ...`.
