"""Hidden "about" easter egg content for the TUI.

This module is the packaged source of truth for the technical overview shown by
the TUI's hidden About screen (so it works even when pip-installed, where the
``docs/`` tree isn't shipped). ``docs/ABOUT.md`` mirrors this text for repo
readers.

Reveal it in the TUI with the undocumented chord **Ctrl+G**, or by typing the
magic word **xyzzy** into the composer. 73!
"""

from __future__ import annotations

ABOUT_MD = """\
# Radio_App — Technical Overview

> You found the easter egg. *Ctrl+G* (or typing **xyzzy**) summoned this.

A minimal-dependency, **Python-only** client-side aggregator that presents many
radio backends as one honest, offline-capable messaging surface — a *single pane
of glass* for radio comms.

## Languages & stack

- **Application code** (~19k lines): **Python 3.10+**, fully type-hinted, async.
- **TUI:** Textual (optional extra). **Config:** TOML (only hard dep: `tomli-w`).
- **Persistence:** SQLite + FTS5 (stdlib). **Reticulum:** `rns` + `lxmf` (extra).
  **MeshCore:** `meshcore` companion lib (extra).
- **Tests:** pytest (766) · ruff · mypy.
- *Interop targets vendored only for reference (not built or shipped here):*
  **Pat** is Go. The app talks to external programs over their network APIs.

The core + CLI run on the **standard library alone**; every transport and the
TUI are opt-in extras.

## Endpoints — created vs. consumed

The app **exposes no inbound server of its own** — no listening port, no API.

**External control endpoints it *consumes* (loopback by default) — 4:**

| Service | Where | Protocol |
|---|---|---|
| rnsd (Reticulum) | local shared-instance socket | RNS shared instance |
| JS8Call | 127.0.0.1:2442 | TCP/JSON |
| Pat (Winlink) | 127.0.0.1:8080 | HTTP/JSON (~10 routes) |
| MeshCore companion | USB serial or 127.0.0.1:5000 | serial / TCP |

**Addressable endpoints it *creates* on the Reticulum network (per run):**
one LXMF delivery destination, plus one shared GROUP destination per configured
group (and the reserved `broadcast` channel), with hashes derived from the
channel name; plus transient RNS Links for NomadNet page fetches.

**Transports:** 5 active built-ins (Reticulum, JS8Call, MeshCore, Winlink,
WSJT-X), plus a plugin entry-point mechanism. **CLI:** ~30 subcommands.

## Author's intent

> One application — a single pane of glass — so the operator doesn't juggle
> MeshChat, JS8Call, a NomadNet browser, etc. Touch-first for a tablet in the
> field, fully usable from a keyboard (SSH to a Pi).

Three convictions: a **transport-agnostic core** (everything is one
`UnifiedMessage`), **offline-first / off-grid**, and **don't lie to the
operator** ("sent" never overstates delivery; encryption blocked on amateur HF;
Health is reachability-only — no test transmissions).

## TUI / GUI relationship to the core

There is **no network API between UI and core** — it's in-process composition.

```
   CLI ─┐
   TUI ─┼──> App ──> Router ──> [ Transport… ] ──> rnsd / JS8Call / Pat / MeshCore
 (GUI) ─┘     │         │
              ├─ MessageStore (SQLite)   ├─ Selector (best transport)
              ├─ FilterEngine            ├─ ComplianceGuard (HF rules)
              └─ Groups / Favorites      └─ Privacy (per-transport identity)
```

The **`App` object is the de-facto API**: any frontend builds one `App(config)`,
calls `router.send(msg)`, and registers a callback for inbound messages. The
**Router** is the seam where dedup, fan-out, retry/ACK, chunking, compliance and
privacy live — so every frontend inherits them for free. A future GUI slots in
identically as a third frontend over the same engine.

## User experience

One inbox, many radios: a single thread can interleave Reticulum, JS8Call and
MeshCore. Operating modes stay calm (contacts only); announce/beacon noise is
quarantined to **Watch**, discovery to **Health**. Honest status throughout:
per-mode reachability dots, ✓✓ only on confirmed delivery, live byte/encryption
indicators in the composer. A slash-command layer keeps power features out of
the default surface.

Utility surfaces cycle on **F5**: Watch (live all-transport feed with bounded
scrollback) → Health (transport reachability + system metrics) → Logs (live
in-process ring buffer, level-filterable) → Chats (all-transport archive).
**Ctrl+F** opens a full-text search palette (FTS5, BM25-ranked, type-ahead).
File attachments flow over Reticulum (LXMF file fields) and Winlink (MIME
multipart); inbound Winlink bodies with base64/QP encoding are auto-decoded to
readable text. The NomadNet browser fetches pages offline-first from a local
cache.

## Simplicity of configuration

**One hand-editable TOML file** holds every setting. Three on-ramps:
`radioapp setup` (interactive wizard), `radioapp config init` (copy the example),
or edit the TOML directly. Single-key edits: `radioapp config get|set <key>
[value]` with smart bool/int/float coercion. Identity is entered **once** under
`[station]` and auto-pushed into every transport that needs it. Everything is
**off by default**, enabled per `[transports.x]` block, with sensible localhost
defaults. Missing libraries or down daemons disable just that transport — the
rest runs — and the Reticulum transport retries `rnsd` in the background and
attaches the moment it appears. `radioapp backup` / `restore` snapshot and
recover the SQLite store + config atomically.

*73 de Radio_App*
"""

