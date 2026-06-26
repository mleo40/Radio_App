"""Seed fake Reticulum (rnsd) chat into the store so you can play with the TUI.

This writes a handful of realistic LXMF-style **direct** conversations straight
into the same SQLite database the app uses, tagged ``transport="reticulum"``.
Launch the TUI afterwards, press **F3** and pick the *reticulum* mode: the seeded
peers appear in the conversation list (left pane). Open one to read the thread;
the Monitor (**F2**) and history behave exactly as with real traffic.

Nothing here touches the radio/RNS stack - it is pure local test data, so it
works with no hardware and without the ``reticulum`` extra installed.

Usage:
    python scripts/seed_fake_chat.py            # seed into your active config DB
    python scripts/seed_fake_chat.py --reset    # wipe seeded rows first
    RADIO_APP_CONFIG=/tmp/demo/config.toml python scripts/seed_fake_chat.py

Then:
    radioapp tui      # F3 -> reticulum, pick a conversation on the left
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime, timedelta

from radio_app.config import Config
from radio_app.core.message import AddressType, DeliveryStatus, UnifiedMessage
from radio_app.core.store import MessageStore

# Our own (fake) anonymous LXMF address - what outbound messages are "from".
OUR_ADDR = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

# Fake peers: (lxmf destination hash hex, friendly label, opening line).
PEERS: list[tuple[str, str]] = [
    ("9f3c1a77b2e84d0516c9a2f8e7d4b031", "lab-node"),
    ("4c8e0d12fa6b47931e5a7c0b9d2f3e64", "kd2abc-bob"),
    ("7a1f9e44c0b83d27516e8a93f2c4d70b", "weather-relay"),
]

# A scripted back-and-forth per peer. ("in", text) = they sent it to us;
# ("out", text) = we sent it to them. Timestamps are spread out automatically.
SCRIPTS: dict[str, list[tuple[str, str]]] = {
    "9f3c1a77b2e84d0516c9a2f8e7d4b031": [
        ("in", "ping over LoRa, you copy?"),
        ("out", "got you 5/9, clean path tonight"),
        ("in", "nice. running the new RNode firmware here"),
        ("out", "how's the battery draw on TX?"),
        ("in", "about 90mA at 17dBm, totally fine on solar"),
    ],
    "4c8e0d12fa6b47931e5a7c0b9d2f3e64": [
        ("in", "hey, you around for the net later?"),
        ("out", "yep, 1900 local on @TTP right?"),
        ("in", "correct. bring the grid square for the log"),
        ("out", "will do, 73"),
    ],
    "7a1f9e44c0b83d27516e8a93f2c4d70b": [
        ("in", "auto: temp 12.4C, wind 8kt NW, baro 1014 falling"),
        ("out", "thanks relay. any rain in the last hour?"),
        ("in", "auto: 0.2mm in last 60min, humidity 78%"),
    ],
}


def _stable_id(peer: str, idx: int) -> str:
    """Deterministic msg_id so re-running the seeder doesn't duplicate rows."""
    return hashlib.sha1(f"seed:{peer}:{idx}".encode()).hexdigest()


def _build_messages(now: datetime) -> list[UnifiedMessage]:
    msgs: list[UnifiedMessage] = []
    # Walk peers oldest-first so the conversation list orders sensibly.
    base = now - timedelta(hours=6)
    minute = 0
    for peer, label in PEERS:
        for idx, (direction, text) in enumerate(SCRIPTS[peer]):
            ts = base + timedelta(minutes=minute)
            minute += 7  # space messages out
            if direction == "in":
                msg = UnifiedMessage(
                    sender=peer,
                    content=text,
                    address_type=AddressType.DIRECT,
                    recipient=OUR_ADDR,
                    transport="reticulum",
                    status=DeliveryStatus.RECEIVED,
                    msg_id=_stable_id(peer, idx),
                    timestamp=ts,
                    metadata={
                        "encrypted": True,
                        "rns_source": peer,
                        "display_name": label,
                        "seed": True,
                    },
                )
            else:
                msg = UnifiedMessage(
                    sender=OUR_ADDR,
                    content=text,
                    address_type=AddressType.DIRECT,
                    recipient=peer,
                    transport="reticulum",
                    status=DeliveryStatus.SENT,
                    msg_id=_stable_id(peer, idx),
                    timestamp=ts,
                    metadata={"encrypted": True, "seed": True},
                )
            msgs.append(msg)
    return msgs


def _reset(store: MessageStore) -> int:
    """Delete previously seeded rows (metadata has seed=True)."""
    cur = store._conn.execute("SELECT msg_id, metadata FROM messages")
    import json

    seeded = [
        r["msg_id"]
        for r in cur.fetchall()
        if json.loads(r["metadata"] or "{}").get("seed")
    ]
    store._conn.executemany(
        "DELETE FROM messages WHERE msg_id = ?", ((m,) for m in seeded)
    )
    store._conn.commit()
    return len(seeded)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="path to config.toml (else the default)")
    parser.add_argument(
        "--reset", action="store_true", help="remove previously seeded rows first"
    )
    args = parser.parse_args()

    config = Config.load(args.config)
    db_path = config.database_path()
    store = MessageStore(db_path)
    print(f"database: {db_path}")

    if args.reset:
        removed = _reset(store)
        print(f"reset: removed {removed} previously seeded message(s)")

    inserted = 0
    for msg in _build_messages(datetime.now(UTC)):
        if store.save(msg):
            inserted += 1
    store.close()

    print(f"seeded {inserted} new reticulum message(s) across {len(PEERS)} peers:")
    for peer, label in PEERS:
        print(f"  - {label:<14} <{peer[:12]}...>")
    print("\nNext: run 'radioapp tui', press F3 and choose 'reticulum',")
    print("then pick a conversation on the left to read/reply.")


if __name__ == "__main__":
    main()

