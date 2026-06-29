# Radio_App — Technical Overview

> Mirror of the in-app hidden "About" screen
> (`src/radio_app/ui/about.py`). Reveal it in the TUI with the undocumented
> chord **Ctrl+G**, or by typing the magic word **xyzzy** into the composer. 73!

A minimal-dependency, **Python-only** client-side aggregator that presents many
radio backends as one honest, offline-capable messaging surface — a *single pane
of glass* for radio comms.

## Languages & stack

| Layer | Technology |
|---|---|
| Application code (~12k lines) | **Python 3.10+**, fully type-hinted, async (`asyncio`) |
| TUI | **Textual** (optional extra) |
| Config | **TOML** (`tomllib` reads, `tomli-w` writes — the only hard dependency) |
| Persistence | **SQLite** (stdlib `sqlite3`) |
| Reticulum transport | `rns` + `lxmf` (optional extra) |
| MeshCore transport | `meshcore` companion library (optional extra) |
| Tests | `pytest` (834) · `ruff` · `mypy` |
| Interop targets vendored for reference (not built or shipped) | **Pat** = Go |

The app's own code is **100% Python**. The Go (`pat/`) tree is the external
program it talks to over its network API — installed and run by the user, not
compiled or bundled here. The core + CLI run on the
**standard library alone** (plus `tomli-w`); every transport and the TUI are
opt-in extras.

## Endpoints — created vs. consumed

The app **exposes no inbound server of its own** — no listening port, no REST
API, nothing it binds. The only `HTTPServer` in the repo is a *fake Pat* inside
test/demo scripts.

**A. External control endpoints it _consumes_ (loopback by default) — 4:**

| Service | Where | Protocol |
|---|---|---|
| **rnsd** (Reticulum daemon) | local shared-instance socket | RNS shared instance |
| **JS8Call** | `127.0.0.1:2442` | TCP/JSON API |
| **Pat** (Winlink) | `http://127.0.0.1:8080` | HTTP/JSON |
| **MeshCore companion** | USB serial (`/dev/ttyACM0`) or `127.0.0.1:5000` | serial / TCP |
| **WSJT-X** | `0.0.0.0:2237` (inbound) | UDP datagrams |

Within Pat it drives **~10 distinct HTTP routes** (`/api/status`, `/api/connect`,
`/api/mailbox/in`, `/api/mailbox/out`, `/{mid}`, `/{mid}/{attachment}`,
`/api/formcatalog`, `/api/form`, `/api/template`, `/api/formsUpdate`).

**B. Addressable endpoints it _creates_ on the Reticulum network (per run):**

- **1 LXMF delivery destination** — its own anonymous messaging address.
- **N shared GROUP destinations** — the reserved `broadcast` channel plus one per
  configured group (IN + OUT), hashes derived deterministically from the name.
- Transient **RNS Links** opened outbound for NomadNet page fetches.

**C. CLI surface:** ~18 subcommands (`send, threads, read, listen, groups, sub,
status, transports, config, tui, setup, reticulum, winlink, js8, fav, browse,
nodes, peers`).

**Transports:** 5 active built-ins (Reticulum, JS8Call, MeshCore, Winlink,
WSJT-X), plus a plugin mechanism — third-party transports load via the
`radio_app.transports` entry-point group with zero core changes.
**ProcManager** (`core/proc_manager.py`) handles on-demand process lifecycle
(spawn/stop for JS8Call, WSJT-X, Pat) via ⚡ Start buttons in the TUI and
`radioapp start <transport>` in the CLI.

## Author's intent

> One application — a **single pane of glass** for radio comms — so the operator
> does not juggle MeshChat, JS8Call, a NomadNet browser, etc. Designed
> **touch-first for a small screen / tablet in the field**, while remaining fully
> usable from a keyboard (e.g. SSH to a Pi).

Three convictions run through the architecture:

1. **Transport-agnostic core.** Everything normalizes to one `UnifiedMessage`;
   the router, store, filters and UI never know which medium carried a message.
   Adding a platform is "a single new class — no changes to the core."
2. **Offline-first / off-grid.** Minimal dependencies, SQLite history for every
   mode, anonymous-by-default on Reticulum, auto-reconnect to rnsd, app-level
   chunking — all push toward "works when the internet doesn't."
3. **Don't lie to the operator.** "Sent" never overstates delivery; encryption is
   blocked on amateur HF by a compliance guard; Health is reachability-only with
   **no test transmissions**; cached/degraded states are labeled.

## How the TUI / GUI relate to the core

There is **no network API between UI and core** — the relationship is in-process
composition, and that's the whole point.

```
        CLI ─┐
        TUI ─┼──> App ──> Router ──> [ Transport, Transport, … ] ──> rnsd / JS8Call / Pat / MeshCore
   (GUI) ────┘     │         │
                   ├─ MessageStore (SQLite)   ├─ Selector (best-transport)
                   ├─ FilterEngine            ├─ ComplianceGuard (HF rules)
                   ├─ GroupRegistry           └─ Privacy (per-transport identity)
                   └─ Favorites / Station
```

- The **`App` object is the de-facto API**: any frontend builds one
  `App(config)` and calls `router.send(msg)` / registers an inbound callback. The
  TUI is literally one such consumer.
- **`Router` is the seam.** Dedup, fan-out, retry/ACK, chunking, compliance and
  privacy all live behind it, so every frontend inherits them for free.
- A **future GUI** slots in identically: a third frontend constructing the same
  `App`, calling the same `router.send` / callbacks. The SVG mockup
  (`docs/gui_mockup.svg`) mirrors the TUI's concepts because both render the same
  underlying model — only the widget toolkit differs.

## User experience

- **One inbox, many radios.** A single conversation thread can interleave
  Reticulum, JS8Call and MeshCore transparently; the user picks a *mode*, not a
  wire format.
- **Operating modes are calm** — contacts/conversations only; announce/beacon
  noise is quarantined to a dedicated **Watch** feed, discovery to **Health**.
- **Adaptive interaction** — touch-first with large targets on a tablet, compact
  for keyboard/SSH, with a `⌨/☞` indicator inferred from input events.
- **Honest status** — per-mode reachability dots, ✓✓ only on confirmed delivery,
  live byte/limit + encryption indicators in the composer.
- **Slash-command power-user layer** over a simple chat box.

## Simplicity of end-user configuration

**Two TOML files, one for operators.** A distributor-level `config.dist.toml`
(baked into the package, optional) and the operator's
`~/.config/radio_app/config.toml` merge in order — the user file always wins. As
an operator you only ever touch one file (overridable via `RADIO_APP_CONFIG`).
Three on-ramps: `radioapp setup` (interactive wizard), `radioapp config init`
(copy the example), or hand-edit the TOML.

- **Identity entered once** under `[station]`, auto-pushed into every transport
  that needs it — no per-transport duplication.
- **Everything off by default**, enabled per `[transports.x] enabled = true`.
- **Sensible localhost defaults** (Pat 8080, JS8Call 2442, MeshCore serial/5000).
- **Graceful degradation** — a missing library or down daemon disables just that
  transport; the rest runs. Reticulum even retries rnsd in the background and
  attaches automatically when it appears.

*73 de Radio_App*

