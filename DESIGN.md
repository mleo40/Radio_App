# Radio_App — Interface Redesign (Single Pane of Glass)

Status: **agreed design, implementation in progress.** Supersedes the older
"active mode + shared Monitor" TUI framing.

## Goal

One application — a **single pane of glass** for radio comms — so the operator
does not juggle MeshChat, JS8Call, a NomadNet browser, etc. The operator
**selects a mode** (transport/workspace) and the app presents the tools that
mode supports. Designed **touch-first for a small screen / tablet in the
field**, while remaining fully usable from a keyboard (e.g. SSH to a Pi).

## Core mental model: Mode = workspace

A persistent **mode selector** is the primary control. Selecting a mode re-skins
the whole workspace to that mode's purpose and toolset.

```
┌──────────────────────────────────────────────────────────────┐
│ ① RETICULUM● ② MESH● ③ JS8● ④ NOMADNET● │ ◷ WATCH  ✚ HEALTH ⌨│
├──────────────────────────────────────────────────────────────┤
│                    MODE WORKSPACE                              │
├──────────────────────────────────────────────────────────────┤
│              [mode action bar / composer]                      │
└──────────────────────────────────────────────────────────────┘
```

### Modes

| Mode          | Surface                  | Tools                                          | Notes |
|---------------|--------------------------|------------------------------------------------|-------|
| **Reticulum** | Contacts + chat          | direct E2E messaging; anonymous identity; show-my-address, announce-now, find-path; delivery receipts | LXMF; existing |
| **Mesh**      | Contacts + chat          | direct/channel msgs; node info; announce (advert) | MeshCore — new transport (USB serial or TCP) |
| **JS8**       | Contacts/groups + chat   | callsign ID, SNR/freq inline, encryption guard | HF |
| **NomadNet**  | Bookmarks + page browser | browse/follow links, dynamic vars              | own mode; promote BrowseScreen |
| **Watch**     | Live feed                | read-only all-transport traffic; pause/clear   | NOT stored |
| **Health**    | Status board             | reachability; "recently heard → add contact"   | reachability only |

Locked decisions: Mesh/Reticulum are **two separate modes**; Watch is a
**dedicated mode**; Health = **reachability only** (no test TX); NomadNet is its
**own mode**; MeshCore connects via **USB serial or TCP**.

## Operating principle: targeted, contacts-only

Operating modes show **contacts/conversations only — no announcements**.
Discovery feeds only "recently heard → add contact" in **Health**.
Announce/advert traffic appears only in **Watch**.

## Health = reachability (no transmissions)

| Mode       | Probe (no TX)                                                |
|------------|--------------------------------------------------------------|
| Reticulum  | RNS attached to shared instance / has interfaces; rnsd reachable |
| JS8        | TCP connect to JS8Call API holds                             |
| Mercury    | Control-socket connect                                       |
| MeshCore   | USB serial opens / TCP socket connects                       |
| NomadNet   | N/A (rides Reticulum)                                        |

Per-mode result **OK / DOWN / N/A** → selector dot (● green / ● red / ○) +
detail in Health with manual "re-check". Announce/traffic counters are a
"hearing the network" signal (surfaced in Watch), **not** the health source.

## Input-mode adaptation

Infer input method from runtime events (pointer/tap vs key); show `⌨ / ☞`
indicator; adapt density (keyboard → compact, touch → large targets) with a
manual override. Caveat: detection is **inferred from input events**, not a
hardware capability query.

## Keybinding / touch map

- Modes: tap or `1`–`4`; Watch `5`/`w`; Health `6`/`h`.
- Chat: `n` new/select contact; type to send; `↑/↓` scroll.
- NomadNet: number to follow link; address to navigate; `b` back; `r` reload.
- Global: `q` quit; `r` / pull-to-refresh re-checks reachability.

## Implementation plan (ordered)

### Milestone 1 — Mode-as-workspace shell (no new transports)
1. `transports/base.py`: add `async check_reachable() -> ReachabilityStatus`
   (default from `running`) + a `surface` descriptor (`"chat" | "browse"`).
2. `ui/tui.py`: replace F3 modal + `#modebar` with a **persistent mode selector**
   driving a `ContentSwitcher` of surfaces.
3. Wire per-mode **reachability dots** (timer-driven).
4. Add **input-mode detection** + `⌨/☞` indicator + compact/touch CSS variants.

### Milestone 2 — Surfaces
5. **Chat surface** (Reticulum/JS8/Mesh): contacts-only list + composer.
6. **NomadNet surface**: reparent `BrowseScreen` + bookmarks.
7. **Watch mode**: all-transport read-only feed (reuse Monitor plumbing minus
   storage/alerts) with pause/clear.
8. **Health mode**: reachability board + "recently heard → add contact".

> Milestone 2 status (in progress):
> - 5 ✅ operating modes are contacts/conversations only (announces never appear
>   there; favorites-only firehose framing is confined to Watch).
> - 6 ✅ NomadNet is now its own mode chip with a node list + address bar; opening
>   a page uses the existing `BrowseScreen`. (Full *inline* reparent of the
>   browser into the surface is a later polish item.)
> - 7 ✅ Watch has Pause/Resume + Clear.
> - 8 ✅ Health shows a reachability board **and** a "recently heard → add
>   contact" discovery list (announce-fed; add turns a station into a favorite).
>   The System section has been significantly extended: UTC clock with
>   multi-source time consensus (GPS via gpsd → local chrony/ntpd → internet
>   NTP, refreshed every 60 s; never starts/manages daemons); station position
>   and Maidenhead grid (from `[position]` config or live gpsd); and host
>   battery level with estimated runtime (Linux sysfs, no extra deps).

### Milestone 3 — MeshCore transport (separate)
9. New `transports/meshcore_transport.py` implementing `Transport` +
   `check_reachable()`, with **USB serial / TCP** backends sharing one framing
   layer; selected via `[transports.meshcore]`. Slots into **Mesh** mode.

> Milestone 3 status: ✅ **done.** `meshcore_transport.py` integrates the official
> `meshcore` Python library (USB serial / TCP companion), implementing the full
> companion protocol: direct messages → MeshCore contacts, groups → channels,
> and inbound `CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV` events mapped to
> `UnifiedMessage`. Node adverts surface as `kind='announce'` telemetry for
> Watch/Health/favorites. Capabilities mark it ISM/encrypted (exempt from the HF
> callsign/no-encryption rules); the passive reachability probe is TCP-connect or
> serial-device-present. The library is an optional extra
> (`pip install 'radio-app[meshcore]'`); absent it, the transport stays disabled.
>
> Mesh-panel follow-ups (done): device **channels** are enumerated at startup
> (and nameable in config) and listed as `#name` conversations; channels can be
> added live from the panel with `/channel add <index> <#name> [secret]` — a
> `#name` **hashtag channel** needs no secret (MeshCore derives the key from the
> name); the panel has an **announce** action bar (zero-hop / flood adverts, also
> `Ctrl+N`); both inbound and outbound mesh chats appear in **Watch**; and
> **Health** shows the companion's battery + LoRa radio parameters alongside
> reachability.

## Config additions (sketch)

```toml
[transports.meshcore]
enabled = false
connection = "serial"   # "serial" (USB) or "tcp"
port = "/dev/ttyACM0"   # serial
baud = 115200           # serial
host = "127.0.0.1"      # tcp
tcp_port = 5000         # tcp
```

## Out of scope / cut

- Favorites-only **firehose Monitor** as a primary surface (→ Watch + Health).
- Random announcements in operating modes.
- Test transmissions for health (reachability only, for now).

