"""Reference screen content: radio + mode setup how-to for field use.

Packaged source of truth for the TUI's F1 Reference screen (works even when
pip-installed — no external docs/ files needed). This is necessarily general
guidance: always cross-check the manufacturer's manual and your installed
Hamlib version's actual rig list, since exact menu wording and available rig
models drift between releases.
"""

from __future__ import annotations

TRUSDX_MD = """\
# (tr)uSDX

A tiny multi-band QRP transceiver. **Single USB cable** carries CAT *and*
power, but — unlike QMX/QDX — it has **no real USB sound card**, so digital
modes need a small helper running alongside JS8Call/WSJT-X.

## The catch: audio needs a bridge script

(tr)uSDX exposes a CH340 USB-serial port only. To get RX/TX audio over that
same cable, run a community bridge script that creates a virtual sound
device and forwards audio to/from the radio:

- **trusdx-audio** (Python, Linux/Pi-friendly):
  github.com/olgierd/trusdx-audio
- Needs `pyserial` + `pyaudio` + PulseAudio. Typical flow:
  1. `pactl load-module module-null-sink sink_name=TRUSDX
     sink_properties=device.description=TRUSDX`
  2. Run `trusdx-txrx.py` (keep it running for the whole session)
  3. In JS8Call/WSJT-X, pick the **TRUSDX** sink/source as your sound device
- TX/RX switching is automatic, VOX-style — silence means receive, audio
  means transmit. No separate PTT wiring needed.
- Radio_App does **not** manage this script for you — start it yourself
  *before* opening JS8Call/WSJT-X, and leave it running for the session.

## CAT control

Emulates a **Kenwood TS-480** command set. In JS8Call/WSJT-X, select
**Kenwood TS-480** as the rig, point it at the CH340 serial port, baud
**115200** (firmware 2.00+ — older firmware may differ, check yours).

## Field notes

- USB-bus-powered draws only a few hundred mA — fine from a laptop/power
  bank; no need for a full 12V supply just to test.
- Audio quality over the bridge is noticeably worse than a real sound card —
  expect a bit more decode noise than QMX/QDX.
- References: dl2man.de/4-trusdx-manual (community manual),
  github.com/miltonics/truSDX-Linux (alternative Linux control software with
  JS8Call integration).
"""

QMX_QDX_MD = """\
# QMX / QDX (QRP Labs)

Genuine **plug-and-play** QRP transceivers — unlike (tr)uSDX, both present a
real **USB Audio Class sound card** *and* a **CDC-ACM virtual serial port**
over the one USB cable. No bridge script, no extra driver needed on Linux.

- **QDX** — 4 bands (80/40/30/20m), **digital modes only** (no SSB/CW).
- **QMX** — broader band coverage, adds **SSB and CW** alongside digital
  modes (QMX+ variants extend band coverage further).

## Setup

1. Plug in — Linux enumerates both the sound card and the serial port
   natively (`arecord -l` / `ls /dev/ttyACM*` to confirm).
2. In JS8Call/WSJT-X, pick the enumerated **QDX**/**QMX** sound device
   directly as your audio in/out.
3. CAT: select the enumerated serial port. **Prefer the native Hamlib rig
   entry** — recent Hamlib versions ship a dedicated **"QRPLabs QCX/QDX"**
   rig model, which tends to be more reliable than forcing Kenwood
   emulation. If your Hamlib is older and only has Kenwood models, the
   manual's fallback is **Kenwood TS-440** (some report TS-480 also works) —
   check your specific Hamlib version's rig list, results vary.
4. PTT method: **CAT** (not VOX/RTS/DTR).

## Field notes

- No separate audio interface or CAT cable to carry — one USB cable is the
  whole interconnect, which matters a lot for a minimal go-kit.
- Manuals: qrp-labs.com/qmx · qrp-labs.com/qdx (operation + CAT command
  reference PDFs linked from each page).
"""

JS8CALL_MD = """\
# JS8Call settings

Radio_App talks to JS8Call over its **TCP/JSON API**, not CAT directly —
JS8Call itself owns the radio.

## Required for Radio_App to connect

**File → Settings → Reporting → Enable TCP Server API**, default port
**2442**. Radio_App's default config already expects `127.0.0.1:2442` — no
change needed there unless you moved JS8Call to another host/port.

## Rig / CAT (JS8Call drives the radio, Radio_App just relays)

**File → Settings → Radio** — select your rig (Kenwood TS-480 for (tr)uSDX;
the native "QRPLabs QCX/QDX" entry if your Hamlib has it, for QMX/QDX), the
serial port, and PTT method **CAT**.

## Audio

**File → Settings → Audio** — pick the radio's sound device (the TRUSDX
virtual sink for (tr)uSDX, or the enumerated QMX/QDX device) as both input
and output.

## Useful once connected

- `/band`, `/freq`, `/bandscan` (Radio_App commands) all drive JS8Call's dial
  remotely — no need to touch JS8Call's own UI to change bands.
- 📍 Beacon button sets your grid in JS8Call so it rides along on normal
  transmissions.
- 📣 CQ / 💓 HB buttons (JS8Call action bar) — a plain CQ call, or a JS8Call
  heartbeat (auto-answered with your SNR by any listening station with
  heartbeat-ack on — the same mechanism `/bandscan` uses per band).
"""

WSJTX_MD = """\
# WSJT-X settings

Radio_App receives FT8/FT4 decodes via WSJT-X's **UDP broadcast**, and can
send free-text replies back to it the same way.

## Required for Radio_App to connect

**Settings → Reporting → UDP Server**: enable it, host `127.0.0.1`, port
**2237** (Radio_App's default). Also enable **"Accept UDP requests"** if you
want Radio_App able to send text through WSJT-X.

## Rig / CAT

**Settings → Radio** — Rig: Kenwood TS-480 for (tr)uSDX; the native "QRPLabs
QCX/QDX" Hamlib entry for QMX/QDX where available (fallback Kenwood TS-440 on
older Hamlib — confirm against your installed version). PTT method **CAT**,
not VOX — VOX is unreliable for FT8's precisely timed transmit windows.

## Audio

**Settings → Audio** — pick the radio's sound device (TRUSDX virtual sink,
or the enumerated QMX/QDX device) as both input and output. Watch the signal
level bar: WSJT-X wants a moderate RX level, not pinned/clipping.

## Mode choice for QRP

FT8 is the most forgiving in poor conditions (decodes well down to very low
SNR) and is the default most operators reach for. FT4 is faster (shorter
sequences) when the band is stronger and more contacts per minute matters
more than weak-signal margin.
"""

OTHER_MODES_MD = """\
# Other modes (quick reference)

## Reticulum

Radio_App never starts its own Reticulum instance — it attaches to an
already-running **rnsd**. Configure your interface (RNode, TCP, serial) in
`~/.reticulum/config`; `radioapp reticulum setup-rnode` writes an RNode
interface for you interactively. No internet required — RNS runs fine over
LoRa/serial alone.

## MeshCore

Connects to a companion MeshCore device over **USB serial or TCP**
(`[transports.meshcore]` in config: `connection = "serial"` or `"tcp"`).
Fully offline — ISM-band LoRa mesh, no internet or external gateway needed.

## Winlink

Wraps **Pat** over its local HTTP API (`pat http`, port 8080 by default).
Pat itself owns the actual bearer (telnet/ARDOP/VARA/packet) — see the
Winlink section of the README for connect-method details.
"""
