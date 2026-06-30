# Feature Request Queue

Tracked, not-yet-implemented feature requests. Newest at the top.

## Cross-mode identity linking ("contacts book")

**Requested:** 2026-06-29
**Status:** Implemented (2026-06-29, `phase1-ux-gaps` branch)
**Area:** `core/contacts.py` (new), `core/store.py`, `app.py`, `ui/tui.py`, `cli.py`

Associate multiple transport-specific addresses to a single named person. Once
linked, marking any of their identities as a favorite in one mode automatically
favorites them in all modes, and display names propagate everywhere.

**Use case:** Bob uses `KC1BOB` on JS8Call, `a1b2c3d4...` on MeshCore, and
`bob@winlink.org` on Winlink. Today you must save three separate favorites across
three modes and they show as unrelated entries. With identity linking, saving any
one of Bob's addresses saves all of them, and every conversation with Bob — across
all transports — shows his name.

**Design notes:**

*Data model* — new `contacts` table in SQLite, separate from `favorites`:
- `contact_id` (UUID), `display_name` (text), `notes` (text), `created_at`
- `contact_identities` table: `contact_id`, `transport` (e.g. `"js8call"`),
  `address` (the transport-specific id — callsign / hex hash / email), `label`
- Many-to-one: one contact can have multiple identities across any number of
  transports. An identity can belong to at most one contact.

*Favorites integration* — `favorites.py` consults the contacts table on add/remove:
when you favorite an address that belongs to a contact, all of that contact's
identities are favorited simultaneously. `display_id()` in the TUI checks contacts
before falling back to the raw address, so names appear everywhere.

*Account management surface* — new hidden TUI view (not in the mode bar; reachable
via `/contacts` command). The view has two panes:
- Left: contact list (display names, sortable). New / Delete / Rename actions.
- Right: identity table for the selected contact. Shows transport + address +
  label. Add (type `<transport> <address> [label]`), Remove, and a search box that
  queries the message store for addresses not yet linked to any contact so you can
  bulk-assign them (e.g. "unlinked addresses heard on meshcore").

*Discovery assist* — when opening a direct thread with an address, check whether
any *other* transport has a message from the same contact (by display name match or
operator-confirmed link). If yes, offer a one-press "link to existing contact" banner.

*CLI* — `radioapp contacts list|add|link|unlink|rename|show <name>`:
```bash
radioapp contacts add "Bob Smith"
radioapp contacts link "Bob Smith" js8call KC1BOB
radioapp contacts link "Bob Smith" meshcore a1b2c3d4
radioapp contacts link "Bob Smith" winlink bob@winlink.org
radioapp contacts show "Bob Smith"
```

*Scope boundary* — identity linking is opt-in and operator-confirmed; the app never
auto-merges addresses based on heuristics alone, since a false merge (two different
people with similar callsigns) is worse than two separate entries.

---

## Scheduled band changes (JS8Call / HF)

**Requested:** 2026-06-29
**Status:** Implemented (2026-06-29, `phase1-ux-gaps` branch)
**Area:** `transports/js8call_transport.py`, `ui/tui.py`, `cli.py`

Change the active HF band on a schedule — for example, move to 40m at 20:00 local
for the evening EmComm net, then back to 20m at 08:00 the next morning.

**Use case:** EmComm nets operate on fixed band/time schedules (e.g. 80m at night,
20m during the day, 40m for regional coverage). Today the operator must manually
switch bands or add a separate cron job. A built-in schedule would keep the radio
on the right frequency for each net window automatically.

**Design notes:**
- New schedule entry type `kind = "band_change"` alongside existing
  `kind = "message"` in `store.scheduled_messages`; fields: `transport`, `band`
  (e.g. `"40m"`), `recurrence` (`"daily"` / `"weekly"` / `"once"`)
- Scheduler loop (already running in `app.py`) checks for due band-change entries
  and calls `js8call_transport.set_band(band)` (already wired; the `radioapp js8
  band <band>` CLI command uses it)
- TUI: extend `/sched` compose flow with a "band change" option alongside the
  existing message scheduler; show pending band-change entries in the schedule list
- CLI: `radioapp schedule band <band> <time> [--daily] [--transport js8call]`
- The existing `JS8_BAND_DIAL_HZ` lookup and `set_band()` implementation in
  `js8call_transport.py` already does the heavy lifting; this is primarily a
  scheduler + UX addition

**Command sketch:**
```bash
radioapp schedule band 40m 20:00 --daily        # every night at 20:00 local
radioapp schedule band 20m 08:00 --daily        # back to 20m at 08:00
radioapp schedule list                          # shows both message and band entries
```

---

## Database merge (`radioapp db merge`)

**Requested:** 2026-06-27
**Status:** Backlog
**Area:** `cli.py`, `core/store.py`

Reconcile two diverged databases after field use. Operator takes a backup to the
field, accumulates offline activity, returns home and merges both copies into one.

**Use case:** Base station runs 24/7 capturing all HF/Reticulum traffic. Operator
copies the database to a field device at departure. Both continue accumulating data
independently. On return, `radioapp db merge field-backup.zip` absorbs the field
copy into the home database without losing anything from either side.

**Design notes:**
- `INSERT OR IGNORE INTO messages SELECT * FROM field.messages` — `msg_id` (UUID
  primary key) handles duplicates automatically; messages present on both sides are
  skipped, unique messages from either side are absorbed
- Merge order across tables (dependency-safe): groups → group_members → group_tags
  → messages → favorites → scheduled_messages
- FTS5 index rebuild required after merge:
  `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`
- Report after merge: "absorbed N new messages from field copy (M already present)"
- Favorites and group membership: union-merge (add entries not already present;
  never delete existing entries from either side)
- Scheduled messages: deduplicate by `msg_id`; if a scheduled send fired on the
  field device it will already be in the messages table, so the scheduled entry can
  be dropped on merge
- Input: accepts same formats as `radioapp db restore` (a `.zip` backup archive or
  a raw `.db` file)

**Command:**
```bash
radioapp db merge field-backup.zip          # merge into current config's database
radioapp db merge field.db --dry-run        # show what would be absorbed, don't write
```

---

## Server-side message forwarding via LoRa/Reticulum

**Requested:** 2026-06-27
**Status:** Backlog
**Area:** `core/router.py`, `transports/reticulum_transport.py`, `config.py`, `cli.py`

A 24/7 base-station instance receives traffic on any transport (JS8Call, Winlink,
WSJT-X, MeshCore, etc.) and automatically forwards matching messages to a field
operator's Reticulum/LoRa node, enabling real-time alerts without internet or SSH.

**Use case:** Server sits at home/EOC with all HF transports running. Operator is
in the field with only an RNode (LoRa). When the server hears traffic addressed to
the operator (or matching a configured filter — group, keyword, sender), it pushes
a compact summary to the field RNS identity over LoRa.

**Design notes:**
- Config: `[forwarding]` section with `enabled`, `destination` (RNS hash of field
  device), `filter` (same filter syntax as existing `[[filters]]` rules), and
  `transport = "reticulum"` (initially Reticulum-only, since it's the only
  anonymous/routed transport)
- Trigger: hook into `Router`'s existing `add_ui_callback()` path — every inbound
  `UnifiedMessage` that passes the filter gets forwarded via the Reticulum transport
- Forwarding message: compact LXMF direct message containing transport source,
  sender callsign, group (if any), and a truncated content preview (≤200 chars to
  fit a LoRa packet budget)
- Field device just needs `radioapp tui` running against its own Reticulum identity
  — forwarded messages arrive as normal direct messages
- No new transport needed; reuses `ReticulumTransport.send()` with a synthetic
  `UnifiedMessage.direct(server_name, field_rns_hash, summary)`
- Daemon/headless mode (separate feature) is a prerequisite for clean 24/7
  operation without a TUI

**UX sketch (config):**
```toml
[forwarding]
enabled = true
destination = "aabbccddeeff..."   # field device RNS hash
transport = "reticulum"
# Forward everything, or scope it:
# filter = { group = "EMS" }
# filter = { to = "KC1QKM" }
```

---

## Outbound group fan-out (cross-mode send)

**Requested:** 2026-06-26
**Status:** Backlog (inbound aggregation is done; this is the send side)
**Area:** `core/router.py`, `core/groups.py`, `ui/tui.py`

"Post to EMS across all member transports" — a single compose action that delivers
to every transport where the group has members, respecting per-mode size caps,
HF-encryption compliance rules, and self-echo dedup.

**Context:** The read side is complete — incoming messages from any transport are
stamped with their group(s) and the Watch/archive views aggregate them. What's
missing is the send side: composing once and having the router deliver to all
group transports automatically.

**Design notes:**
- Router enumerates `group.members` by transport, builds one `UnifiedMessage` per
  transport with appropriate addressing (callsign for JS8Call/Winlink, hash for
  Reticulum, broadcast for MeshCore)
- HF transports must gate on `compliance.allow_encrypted_on_hf` — plaintext only
  unless the operator explicitly enables it
- Per-mode size caps enforced at fan-out time; long content is truncated with a
  `[truncated]` suffix for bandwidth-constrained transports (JS8Call 900 bytes,
  FT8 13 chars)
- Self-echo dedup: messages the operator sent are excluded from the incoming
  aggregation so they don't re-appear in the group feed
- TUI: send to a group via `/to @EMS` in the composer; if multiple transports are
  configured for EMS the router fans out silently and logs each delivery attempt

---

## Past-chats: lazy scroll-back for large threads

**Requested:** 2026-06-26
**Status:** Backlog (archive view is done; this is the lazy-load polish)
**Area:** `ui/tui.py`, `core/store.py`

The "All chats" archive view loads full threads into a ListView. For very long
threads (thousands of messages) this is slow and memory-intensive. Lazy scroll-back
would load a window of messages and extend it as the operator scrolls up.

**Design notes:**
- Load most-recent N messages on open (e.g. 100)
- Prepend an older page when the operator scrolls to the top
- A "load more" sentinel item at the top of the list triggers the next page
- `store.query(limit=N, offset=page*N)` already supports pagination; this is
  purely a TUI rendering change
