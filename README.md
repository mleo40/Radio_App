# Radio_App

A single application for **uniform messaging over radio, the internet, and LoRa**.
Messages are normalized into one format regardless of the medium that carried them,
so to the user it doesn't matter whether a message travelled via **Reticulum**
(internet / LoRa / serial), **JS8Call** (HF weak-signal radio), **MeshCore**
(license-free ISM LoRa mesh), **Winlink** (store-and-forward email over radio,
via Pat), or the **Mercury** HF modem.

> Status: **working core + transports**. The core (unified message model, router,
> best-transport selection, group handling, inbound filtering, SQLite persistence,
> config, CLI, Textual TUI) is implemented and tested. The **Reticulum**,
> **JS8Call**, **MeshCore** and **Winlink** transports are functional; the
> **Mercury** adapter still has its wire-level translation marked `TODO`. The app
> runs and the whole suite is testable without any radio hardware.

## Key ideas

- **Unified message format** — `UnifiedMessage` is the only thing the UI sees.
- **Pluggable transports** — every medium implements one `Transport` interface and
  self-registers. Adding a platform (known or unknown) is a single new class, or a
  separate pip package discovered via entry points. No core changes.
- **Groups** (`@TTP`, `@TTPNE`) — a first-class address type, mapped to each
  transport's native mechanism, with **subscriptions + filter rules** controlling
  what you receive.
- **Saved conversations** — every message (any transport) persists to one SQLite
  store, giving uniform, searchable, cross-transport history.
- **Single config file** — all user configuration lives in one TOML file that is
  both hand-editable text and the exact file a future GUI reads/writes.

## Station identity, privacy & compliance

- **Setup wizard** — run `radioapp setup` to capture **all** user settings in one
  pass (display name, Reticulum/`rnsd` location, JS8Call connection, MeshCore
  connection, Winlink/Pat connection, callsign, grid square) and write them to the
  single config file. If JS8Call is enabled and running, the wizard **asks JS8Call
  for your callsign and grid** so you don't retype them (you can still override).
  The radio itself is driven by the transport app (JS8Call), so there is **no
  rig/CAT configuration** in Radio_App.
- **Privacy separation** — your callsign/grid are attached only on **HF transports**
  (where identifying on the air is required). On the **Reticulum** transport the
  sender is replaced with an anonymous cryptographic identity and any operator PII
  (callsign, grid, QTH, name) is **stripped** before sending. This is
  capability-driven (`carries_operator_identity`), so future transports inherit it.
- **Encryption-over-HF guard** — amateur regulations generally prohibit encrypting
  messages to obscure their meaning on the air. The app **refuses** to send an
  encrypted payload over an HF transport unless you both set
  `compliance.allow_encrypted_on_hf = true` **and** confirm an explicit warning at
  send time (a typed `I ACCEPT` in the CLI, or a red confirmation modal in the TUI).
- **Per-protocol size limits** — each transport publishes its documented
  per-message cap (`max_message_size`, e.g. MeshCore **134 bytes**, Winlink
  **120 KB**). The composer enforces the active mode's cap: it **blocks** oversize
  messages on transports with a real limit and shows a live `bytes/limit` counter
  in the status bar. **JS8Call** has no published cap (it auto-frames long text
  into successive transmissions), so it only **warns**. Slash-commands are exempt,
  and the count is measured in **UTF-8 bytes** (an emoji/accent is several).
- **App-level chunking + ACK/retry** — on small-MTU media (**JS8Call** ~80 B,
  **MeshCore** 134 B) the router transparently splits a longer message into
  compact framed parts (`RC|gid|seq/total|…`) and reassembles them on the
  receiver, so you can send bodies larger than one on-air frame. On a medium
  without native delivery confirmation (JS8Call) the receiver returns a tiny ACK
  frame and the sender retransmits only the parts that weren't acknowledged.
  Single-frame messages carry **zero** overhead, and non-chunk traffic is
  untouched. Opt-in per transport via the `supports_chunking` capability.

## Architecture

```
        UI (CLI + Textual TUI; GUI later) — thin layer over the core
                     │  UnifiedMessage
              ┌──────┴──────┐
              │   Router    │  selection · fallback · dedup · filtering · persist
              └──────┬──────┘
       ┌──────────┬──────────┬──────────┬──────────┐
  Reticulum   JS8Call   MeshCore   Winlink    Mercury   (+ plugin transports)
 internet/LoRa   HF     ISM LoRa   email/RF   HF modem
```

Source layout (`src/` layout, PEP 8):

```
src/radio_app/
├── config.py              # single-file TOML config (text + GUI source of truth)
├── app.py                 # wires config -> store/groups/filters/transports/router
├── cli.py                 # argparse CLI (stdlib only)
├── core/
│   ├── message.py         # UnifiedMessage, AddressType, DeliveryStatus
│   ├── selector.py        # best-transport scoring + SelectionMode
│   ├── router.py          # outbound selection/fallback, inbound dedup/filter/store
│   ├── groups.py          # Group + GroupRegistry (@TTP, subscriptions)
│   ├── filters.py         # inbound filter rule engine
│   └── store.py           # SQLite persistence
├── ui/                    # Textual TUI (single pane of glass)
└── transports/
    ├── base.py            # Transport ABC + TransportCapabilities + auto-registry
    ├── reticulum_transport.py
    ├── js8call_transport.py
    ├── meshcore_transport.py
    ├── winlink_transport.py   # wraps a user-installed Pat client over HTTP
    └── mercury_transport.py
```

## Installation

Requires **Python 3.11+**.

```bash
# Core + CLI only (no radio deps; great for trying the model)
pip install -e .

# With the Reticulum transport (internet / LoRa / serial)
pip install -e ".[reticulum]"

# Everything (Reticulum + TUI deps + NomadNet reference)
pip install -e ".[all]"

# Developer tooling (pytest, ruff, mypy)
pip install -e ".[dev]"
```

External programs (not pip packages) are required for the HF transports:

- **JS8Call** running with its TCP/JSON API enabled (default port 2442) + radio.
- **Mercury** modem running with its control socket + radio.
- **Pat** (Winlink client) running its HTTP API (`pat http`, default port 8080)
  for the **Winlink** transport. Pat is **user-installed and never bundled** —
  this app only talks to it over HTTP (see *Winlink* below).

## Reticulum / RNode (working today)

The Reticulum transport is implemented on **RNS + LXMF** and provides real,
end-to-end-encrypted **direct messaging** over internet, LoRa (RNode) or serial.
Identity here is an **anonymous** Reticulum address — your callsign/grid are never
attached on this medium.

**Groups & broadcast.** Reticulum also supports shared **group channels** and a
**broadcast** channel. A group (e.g. `@TTP`) maps to an RNS **GROUP destination**
whose address *and* encryption key are derived from the channel name — so every
node that knows the name joins the same encrypted channel, exactly like a
MeshCore hashtag channel. Group/broadcast traffic is single-packet (≈300 chars)
and delivered over shared/broadcast interfaces (LoRa mesh, a local segment);
multi-hop transport-routed group delivery would need a propagation node (future).


```bash
pip install -e ".[reticulum]"      # install RNS + LXMF

# Configure your connected RNode (writes an interface into ~/.reticulum/config):
radioapp reticulum setup-rnode     # prompts for port/frequency/bandwidth/SF/CR/power

radioapp reticulum address         # show your anonymous LXMF address (share this)
radioapp reticulum status          # confirm the transport is up
```

To message a peer, use their LXMF address (hex) as the recipient:

```bash
radioapp send --to <peer_lxmf_hex> "hello over LoRa"
```

Notes:
- **The app only ever attaches to an external `rnsd` — it never starts its own
  Reticulum instance.** Run the Reticulum daemon (`rnsd`) and the app connects to
  it over the local shared-instance socket, sharing its RNode/interfaces (exactly
  like `nomadnet` does; only one process can own an RNode). If `rnsd` isn't up
  when the app starts, the Reticulum transport stays down and **keeps retrying in
  the background** (every `reconnect_interval` seconds, default 10) — so the
  moment `rnsd` comes online the transport attaches automatically, no restart
  needed.
- `setup-rnode` never overwrites an existing `~/.reticulum/config`; if you already
  configured Reticulum, edit that file by hand instead.
- Frequency/bandwidth/SF/CR **must match** the other stations in your LoRa network.
- A direct send only succeeds once a path to the peer is known (they must have
  announced). Your own announce goes out on startup (`announce_on_start = true`).
- `scripts/reticulum_smoke.py` brings the stack up and prints your address.

## Winlink (store-and-forward email over radio)

The **Winlink** transport carries email-style store-and-forward messages over
radio (or internet). It works by wrapping **[Pat](https://github.com/la5nta/pat)**,
a mature open-source (MIT) Winlink client, over its HTTP API — the same
"talk to an external app" pattern used for JS8Call and Mercury. **Pat is
user-installed and never bundled**; this app ships only a thin HTTP client, so
there are no extra Python dependencies.

Setup:

```bash
# 1. Install Pat separately (see its README): package, release binary, or build.
# 2. Configure your Winlink callsign/password inside Pat.
# 3. Run Pat's HTTP server:
pat http        # serves the API on 127.0.0.1:8080 by default
```

Then enable the transport in your config:

```toml
[transports.winlink]
enabled = true
pat_url = "http://127.0.0.1:8080"
callsign = "N0CALL"
connect = "telnet"     # internet path — works with no radio/modem
gateway = ""           # RMS gateway callsign for RF; empty = Pat's default CMS
auto_connect = false   # queue to outbox (false) or dial on every send (true)
```

**Connection methods.** How Pat reaches a gateway is the *scheme* of its connect
URL, modelled as a pluggable method. **Telnet** (internet, no radio) ships ready
to use. RF methods are a config change once the matching modem is set up **inside
Pat** — nothing in this app changes:

| `connect` value | Path | Needs |
|---|---|---|
| `telnet` | internet → CMS | nothing (works out of the box) |
| `ardop` | RF | ARDOP soundcard modem |
| `varahf` | RF | VARA HF — **including Mercury** (VARA-compatible TNC) |
| `varafm` | RF | VARA FM |
| `pactor` | RF | SCS Pactor hardware TNC |
| `ax25` | RF | packet TNC / Direwolf |

Because Mercury presents a VARA-compatible TNC, it can serve as the open-source
RF modem under Pat via `connect = "varahf"` — no Wine or closed binaries needed.
The modem, audio routing and PTT all live inside Pat (outside this app), exactly
like the rig does for JS8Call.

**Automatic fallback (recommended).** Set `connect = "auto"` and Winlink tries
each path in `connect_order` (default `telnet → varahf → ardop`), probing each
modem's port first and using the **first that's reachable and connects** — so the
operator never chooses telnet vs Mercury vs VARA; the message just gets out by
whatever path is available. (Mercury and VARA both answer the `varahf` probe, so
they share that slot.) Force a single path any time with `connect = "telnet"`
(etc.), and a picked RMS gateway still overrides everything.

On the **Health** tab, the `winlink` row shows Pat's reachability plus a line per
connection path (telnet, varahf/Mercury, ardop) with an up/down dot from a
passive port probe — so you can see, for example, that the VARA modem is down
even while Pat itself is reachable.

In the TUI, **Winlink is its own mode** — select the `winlink` chip (or F3 to
it). The mode shows a **Winlink action bar** summarising the connection method
and gateway, with buttons:

- **✎ Subject** — set the subject for the next message (or type `/subject <text>`).
- **📡 Connect** — start a Pat session to send the outbox and receive mail
  (`/connect [gateway]`). While the session runs, **live progress** from Pat's
  WebSocket (dialing → connected → tx/rx %) is logged in the message pane.
- **☰ Gateways** — list nearby RMS gateways from Pat (`/gateways`); the callsigns
  are **clickable** — click one to set it as the gateway and connect immediately
  (or `/gateway <CALL>` then Connect).

Address a message with `/to <callsign>` (e.g. `/to W1AW`), type the body, and
send; the pending subject is attached and then cleared. With no conversation
selected the pane shows all received Winlink mail.

**Attachments.** Queue files for the next outbound message with `/attach <path>`
(repeat for several; `/attach` lists the queue, `/attach clear` empties it); they
upload as Winlink attachments when you send. For received mail, `/save` downloads
the attachments of the latest message in the open conversation to your
`download_dir` (default `~/.local/share/radio_app/winlink`). Attachment names are
shown inline with a 📎 marker on both sent and received messages.

**Delivery confirmation.** A sent message is **queued** in Pat's outbox; once a
session actually forwards it (it leaves the outbox) the message is marked
**✓ delivered** in the conversation — so "sent" never overstates delivery.

From the command line, `radioapp winlink` mirrors this: `winlink status` checks
that Pat is reachable and prints the connect method/gateway, `winlink gateways`
lists RMS gateways, and `winlink connect [CALL]` runs a session.

**Winlink forms.** Standard Winlink forms/templates (ICS-213, check-in, position,
weather, …) are supported through Pat: `winlink forms-update` downloads the latest
standard-forms set and `winlink forms` lists the installed templates. Filling in
and sending a form is currently a **programmatic** step — the transport's
`compose_form()` drives Pat's browserless build flow (it generates the
`RMS_Express_Form` XML attachment and queues the completed form in the outbox for
the next session) — but there is **no interactive form composer in the CLI/TUI
yet** (queued; see [`FEATURE_REQUESTS.md`](FEATURE_REQUESTS.md)).

Notes:
- Outbound messages are posted to Pat's **outbox**; with `auto_connect = false`
  they are delivered on the next session you start (manually in Pat, or by a send
  with `auto_connect = true`). Use the message `metadata["subject"]` to set the
  subject; otherwise the first line of the body is used.
- Inbound mail is discovered by polling Pat's inbox (`poll_interval` seconds);
  attachment names are surfaced in `metadata["attachments"]` and downloaded on
  demand with `/save`.
- `connect_url` is a full escape hatch that overrides `connect`/`gateway` with a
  raw Pat connect string (e.g. `ardop://N0XYZ?freq=7100`).
- **Credentials:** your Winlink account password lives **inside Pat** (its own
  secure-login config) and is presented to the CMS by Pat — Radio_App never sees
  or stores it. If a future version sets it on your behalf it will push to Pat's
  config API and keep any local secret in the OS keyring rather than the TOML.

## Configuration

All settings live in one TOML file (see [`config.example.toml`](config.example.toml)).

```bash
radioapp config init     # copy the example to your user config dir
radioapp config path     # show where the app looks
radioapp config show     # print the active config
radioapp setup           # interactive wizard: all user settings, one file
```

Default location: `~/.config/radio_app/config.toml`
(override with `RADIO_APP_CONFIG=/path/to/config.toml`).

### Landing surface (`[ui].home`)

By default the TUI opens on the first configured transport. Set `[ui].home` to
choose a different startup surface:

```toml
[ui]
home = "health"   # or: "watch", "favorites", "nomadnet", or a transport name
                  #     ("meshcore" / "js8call" / "reticulum"). Empty = first mode.
```

An unrecognized value falls back to the first configured mode.

## CLI usage (the command-line subset)

```bash
radioapp send --to N0CALL "meeting at 1900"        # router picks best transport
radioapp send --to N0CALL --mode secure "private"  # require encryption
radioapp send --group TTP "net starts in 5"        # send to a group
radioapp send --to N0CALL --transport js8call "hi" # force a transport

radioapp threads                  # list saved conversations
radioapp read --thread @TTP       # print a group thread
radioapp read --to N0CALL         # print a direct thread
radioapp listen --group TTP       # stream incoming TTP messages

radioapp groups                   # list configured groups (+ subscription mark)
radioapp sub add TTPNE            # subscribe to a group
radioapp transports               # list transports + capabilities
radioapp status                   # what's up / connected
radioapp nodes                    # discovered NomadNet sites
radioapp peers                    # discovered LXMF peers
radioapp winlink status           # check Pat reachability + connect method
radioapp winlink gateways         # list nearby RMS gateways via Pat
radioapp winlink connect [CALL]   # run a Winlink session (send outbox, get mail)
radioapp winlink forms            # list installed Winlink form templates
radioapp winlink forms-update     # download the latest standard forms
radioapp js8 inbox                # list JS8Call's store-and-forward inbox
radioapp js8 cmd W1AW SNR?        # send a directed command to a station
radioapp js8 relay W1AW "qsy 40m" # leave a store-and-forward message for W1AW
radioapp send --to N0CALL --mode secure --encrypt "x"  # guarded on HF
```

Run via the installed `radioapp` script or `python -m radio_app`.

## TUI (terminal user interface)

A Textual-based terminal app designed as a **single pane of glass** — one screen
for all your radio comms instead of juggling separate apps. A persistent **mode
selector** along the top is the primary control: each configured transport is one
operating **mode**, plus two utility surfaces, **Watch** and **Health**. See
[`DESIGN.md`](DESIGN.md) for the full model and roadmap.

- **Operating modes (interact):** tap a mode chip (or **F3**) to choose a
  transport. Sending is **bound to that mode** — no auto-selection. The
  conversation list is scoped to it; the status bar shows capability detail
  (callsign vs. anonymous identity, encrypted vs. plaintext) from
  `TransportCapabilities`. Press **F4** to filter the conversation list to
  **favorites only** for that mode (the open conversation always stays visible);
  the status bar shows `[fav-only]` while active. In **MeshCore** mode the panel
  lists the device's group **channels** and adds an action bar to **announce**
  (broadcast a node advert — **📣 Announce** for a zero-hop advert or **🌊 Flood**
  to propagate across the mesh; **Ctrl+N** also works). In **Reticulum** mode the
  same announce key re-announces your LXMF identity.
  - **JS8Call band bar:** in **JS8** mode the panel shows the **current dial
    frequency and band** (queried live from JS8Call) so you always know which
    band you're on, plus quick band-switch buttons (**80m … 10m**) and a **↻**
    refresh. Switch from the composer too: `/freq` shows the current frequency,
    `/freq 14.078` (MHz) or `/freq 14078000` (Hz) sets it, and `/band 20m`
    jumps to a band's standard JS8 dial frequency. Remote band changes require
    JS8Call to have **CAT/rig control** configured (JS8Call drives the radio).
  - **JS8Call quick-query bar:** a bar at the **bottom** of the JS8 panel sends
    standard JS8 directed queries to the open conversation with one tap —
    **SNR?**, **HEARING?**, **STATUS?**, **INFO?**. Open a callsign to ask one
    station, or an `@GROUP` to ask the whole group (the target prefix is added
    for you, e.g. `@TTP SNR?`).
  - **JS8Call inbox & relay:** `/inbox` lists the messages JS8Call is holding
    for **store-and-forward** relay; `/relay <CALL> <text>` leaves a message
    JS8Call forwards when it next hears that station; and `/cmd [<CALL>] <SNR?|
    GRID?|INFO?|…>` sends any JS8 directed command (the same set is available
    from the CLI: `radioapp js8 inbox|cmd|relay`).
  - **All-messages view:** when **no** callsign or `@group` is selected, the
    JS8 window shows a live **firehose of every JS8Call message** (across all
    conversations) instead of an empty pane, so you can watch the band at a
    glance. New traffic appends live; pick a conversation (click a name, tap a
    row, or `/to <call|@GROUP>`) to focus it, and closing a conversation drops
    back to the firehose. (This applies to any chat mode that has no default
    conversation, e.g. Reticulum too.)
  - **Adding a channel:** in MeshCore mode, type
    `/channel add <index> <#name> [secret]` in the composer (e.g.
    `/channel add 2 #ops`). A **hashtag channel** (a name starting with `#`)
    needs **no secret** — MeshCore derives the channel key from the name, so
    anyone who knows `#ops` can join. The channel is created on the companion
    device, saved to your config (shown as `#ops`), and opened. `/channel list`
    shows the current channels; `/channel rm <index>` drops a saved name.
  - **Channel sender names:** MeshCore channels carry no per-sender identity on
    the wire, so the convention is for each node to prefix its name (`Name: …`).
    Radio_App does this automatically on send (using your device's node name) and
    parses it on receive, so channel messages show **who** sent them and their
    names stay clickable for a direct reply. (Messages with no embedded name show
    as an anonymous channel sender and aren't clickable.)
- **Watch (observe):** select the **Watch** tab for a unified, **read-only** live stream of
  **all** messages across **every** transport — both the traffic you **receive**
  and the messages you **send** (e.g. both sides of a MeshCore channel) —
  regardless of the active mode. Selecting an item opens that conversation and **switches the active mode**
  to its transport, so replying stays deliberate.
- **Health (verify):** press **F5** for passive per-transport **reachability**
  probes (no transmission) — "can we reach `rnsd` / the JS8Call API / the modem
  socket right now?". Each mode chip carries a live health dot:
  **● up · ○ down · · n/a · ◌ unknown**. For **Reticulum** the board adds
  per-interface RNS/RNode telemetry; for **MeshCore** it adds the companion's
  **battery** and **LoRa radio parameters** (frequency / bandwidth / SF / CR /
  TX power).
- **Favorites (recall):** press **F5** to cycle into it (Watch → Health →
  Favorites) for a saved list of **NomadNet servers, callsigns, JS8Call groups,
  MeshCore channels, MeshCore contacts and hashes**, grouped by type. Add entries
  **without the peer being online first** — type
  `[node|peer|call|group|channel|contact] <id> [label]` in the bar and press
  Enter (e.g. `node a1b2… HomeNode` saves a NomadNet server; `@TTP net` saves a
  JS8Call group; `channel ops Ops Net` saves a MeshCore channel by name;
  `contact a1b2c3… Bob` saves a MeshCore user by public-key prefix; the type is
  persisted). You can also favorite the **open conversation** in one step — the
  **★ Favorite** button in the MeshCore action bar, or `/fav here [label]` from
  any chat mode (a MeshCore channel is saved by its `#name`, a contact by its
  pubkey prefix). Enter on a row opens it (browse a node, or start a conversation
  in the right mode). Delete a favorite by selecting its row and clicking
  **Remove** (or **Ctrl+D**, or `/fav rm <id>`). The **⤓ Import JS8 groups**
  button (or `/fav groups`) pulls your JS8Call groups straight from the JS8Call
  API; `radioapp setup` also offers to import them.

```bash
pip install -e ".[tui]"   # install the TUI dependency
radioapp tui              # launch it
```

```
 ① reticulum●  ② js8call○  ③ meshcore○  ④ winlink○   ◷ Watch  ✚ Health   ⌨   <- mode selector
+---------------+------------------------------+
| Conversations |  Messages (active mode)      |   <- operating-mode view
|  (scoped to   |                              |
|   active mode)|                              |
+---------------+------------------------------+
| view / mode / id / target / transports       |   <- status bar
| Type a message or /help ...                  |   <- composer (mode-bound send)
+----------------------------------------------+

 Watch tab: all transports, read-only
   12:01:03 [reticulum] ENC a1b2... -> : ping over LoRa
   12:01:04 [js8call]   --- KE7XYZ  -> @TTP: net in 5
```

Keys: **F3** choose mode · **F4** favorites-only (Watch + every mode) ·
**F5** cycle Watch/Health/Favorites · **Ctrl+R** refresh · **Ctrl+C** quit. The
mode chips are tappable on a touchscreen; tap the **⌨/☞** glyph to toggle a
larger touch layout.
In-composer commands:

| Command | Action |
|---------|--------|
| `/to <callsign>` | start/switch a **direct** conversation (in the active mode) |
| `/to @GROUP` | start/switch a **group** conversation (if the mode supports groups) |
| `/to <callsign\|@GROUP> <message>` | switch **and** immediately send (e.g. `/to @TTP SNR?`) |
| `/channel add <index> <#name> [secret]` | (MeshCore) create/join a channel — a `#name` hashtag channel needs no secret |
| `/channel list` / `/channel rm <index>` | (MeshCore) list channels / drop a saved channel name |
| `/fav add [type] <id> [label]` | add a favorite (`type` = `node\|peer\|call\|group\|channel\|contact`) |
| `/fav here [label]` | favorite the **open conversation** (MeshCore channel by `#name`, contact by pubkey) |
| `/fav list` / `/fav rm <id>` / `/fav only` | list favorites / remove one / toggle the favorites-only filter |
| `/freq` / `/freq <MHz\|Hz>` | (JS8Call) show / set the radio dial frequency |
| `/band` / `/band <name>` | (JS8Call) list bands / switch band (e.g. `/band 20m`) |
| `/subject <text>` | (Winlink) set the subject for the next message |
| `/attach <path>` / `/save` | (Winlink) queue an outbound file / save received attachments |
| `/connect [CALL]` / `/gateways` / `/gateway <CALL>` | (Winlink) run a session / list & pick RMS gateways |
| `/monitor` | toggle the Monitor view |
| `/mode` | reminder to press **F3** to change the active transport |
| `/refresh` | reload conversations |
| `/help`, `/quit` | help / exit |

> **Targeting a MeshCore user:** there is no `@`/bracket syntax — address a
> contact by their **name** or **hex public-key prefix** (e.g. `/to Alice` or
> `/to a1b2c3d4e5f6`). `@<index>` is reserved for **channels** (`@0` = public).
> Callsigns (JS8Call/Mercury) are upper-cased for you; MeshCore names/hashes and
> Reticulum addresses are kept **case-sensitive**.

Typing plain text sends to the selected conversation **over the active mode only**.
Each line shows the transport that carried it (`[js8call]`, `[reticulum]`, ...).
**Click an inbound sender's name** in the message log to open a **direct reply**
to that person — handy in a shared thread (a MeshCore channel or a JS8 `@group`)
where one conversation carries many senders. The message format, SQLite storage
and rendering are identical across modes and in the Monitor.

> Headless walkthroughs: `scripts/monitor_demo.py` (mode-bound send + all-transport
> Monitor) and `scripts/tui_setup_demo.py` (first-run station setup).

Run via the installed `radioapp tui` or `python -m radio_app tui`.


## Development

```bash
pytest        # run the test suite (no hardware needed)
ruff check .  # lint
```

## Adding a new transport

1. Create `src/radio_app/transports/my_transport.py`:
   ```python
   from .base import Transport, TransportCapabilities

   class MyTransport(Transport):
       name = "mytransport"  # setting `name` auto-registers it

       def capabilities(self) -> TransportCapabilities: ...
       async def start(self) -> None: ...
       async def stop(self) -> None: ...
       async def send(self, msg) -> bool: ...
   ```
2. Import it in `transports/__init__.py` (or ship it as a separate package exposing
   a `radio_app.transports` entry point).
3. Enable it with a `[transports.mytransport]` block in the config.

The router, message model, selection, filtering, persistence and UI are untouched.

## Roadmap (not yet implemented)

- Wire-level translation in the Mercury adapter (marked `TODO`).
- RNS `Link`-based **live keyboard-to-keyboard** session path.
- **NomadNet node hosting** (publishing pages). Read-only **page viewing is
  implemented** — see below.
- **Desktop GUI** + visual config editor over the same single config file.
  See [`docs/gui_mockup.svg`](docs/gui_mockup.svg) for an early concept mockup
  (illustrative only — not yet implemented).

See [`FEATURE_REQUESTS.md`](FEATURE_REQUESTS.md) for the queued feature backlog.

## NomadNet pages (read-only viewing)

View NomadNet pages over your existing Reticulum stack — no extra packages
beyond the `reticulum` extra. Viewing is strictly read-only: a built-in micron
renderer displays pages (including **dynamic** pages, by passing `var=value`
request data from links/addresses), but there is no page hosting and no on-page
form submission.

```bash
radioapp nodes --wait 30                 # discover NomadNet nodes from announces
radioapp browse <node_hash>              # fetch /page/index.mu and render it
radioapp browse <node_hash>:/page/x.mu   # a specific page
radioapp browse "<hash>:/page/d.mu|a=1|b=2"   # dynamic page with request vars
radioapp browse <node_hash> --raw        # print raw micron source
```

In the TUI, **NomadNet is its own mode** — select the `nomadnet` chip in the mode
selector to open the surface, which lists discovered nodes (tap/Enter to open one)
and has an address bar (`<hash>[:/page/x.mu]`) to browse directly. Press **F4** to
filter the node list to your **saved/favorite nodes only**. The composer
commands `/nodes` and `/browse <hash>` still work from any mode. Inside the
viewer: type a link number to follow it, enter a new address, `Ctrl+B` back,
`Ctrl+R` reload, `Esc` to close.

## License

This project is licensed under the **MIT License** — see [`LICENSE`](LICENSE).

### External tools & licenses

Radio_App **interoperates with** several external programs over their network
APIs but does **not** bundle or redistribute them — each is installed and run by
the user, and only your own MIT code is distributed here. For reference:

| Tool | Role | License |
|---|---|---|
| **Pat** | Winlink client (wrapped over HTTP) | MIT |
| **JS8Call** | HF weak-signal app (TCP/JSON API) | GPL-3.0 |
| **Mercury** | HF modem / VARA-compatible TNC (TCP) | GPL-3.0 |
| **Reticulum (RNS/LXMF)** | networking stack (pip extra) | MIT/Reticulum |

Talking to these programs over their sockets/APIs is mere aggregation, so no
copyleft obligation attaches to this MIT codebase. The wire protocols themselves
(command sets, JSON shapes) are not copyrightable. Only if you were to *bundle* a
GPL binary would its license terms apply to that distribution.

