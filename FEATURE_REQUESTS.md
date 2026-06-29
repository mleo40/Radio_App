# Feature Request Queue

Tracked, not-yet-implemented feature requests. Newest at the top.

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
- No new transport needed; reuses `ReticululTransport.send()` with a synthetic
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

## On-demand transport app launcher (`/start` command)

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/proc_manager.py`, `ui/tui.py`, `cli.py`

✅ Shipped. `ProcManager` handles full on-demand lifecycle for JS8Call, WSJT-X,
and Pat (Winlink). ⚡ Start buttons in each transport mode bar plus `/start
[transport]` in the composer and `radioapp start <transport>` from the CLI.

**What shipped:**
- `launch_cmd` config key per transport (with built-in defaults: `js8call`,
  `wsjtx`, `pat http`). Arguments supported verbatim.
- First-time `LaunchCmdScreen` modal prompts for a custom command if none is
  configured; answer is saved to config automatically.
- Spawned with `start_new_session=True` — detached, no stdio, no focus steal.
- `pgrep -x <process_name>` as OS truth for `is_running()`; `we_own()` is
  separate — Radio_App only stops processes it started, never external ones.
- Radio interlock transfer on start for radio-contending transports (JS8Call,
  WSJT-X); Winlink is non-contending.
- Polls `check_reachable()` up to `launch_wait_s` (default 15 s) before
  connecting the transport adapter.
- `radioapp start <transport>` CLI equivalent wired up.
- 97 tests covering ProcManager + TUI start command path.

---

## Presence roster — recently-heard callsigns across all transports

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/roster.py`, `cli.py`, `ui/tui.py`

A unified "who's been heard recently" board derived from the message store — no
extra state, no new persistent table. Each entry groups by (callsign, transport)
pair and shows last-seen time, last SNR, message count, and a content preview.

**Delivered:**
1. ✅ **`core/roster.py`** — `PresenceEntry` dataclass + `get_roster(store, since, transport, limit)` implemented as a single SQL aggregation query (correlated subqueries pull last SNR and last content per pair). Defaults to a 24-hour lookback.
2. ✅ **CLI** — `radioapp roster [--transport js8call] [--since 48h] [--limit N]` prints a formatted table (callsign / transport / last seen / SNR / count / preview).
3. ✅ **TUI** — `/roster [Nh]` dumps the roster to the log pane with Rich formatting.

---

## Scheduled / windowed message send

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/store.py`, `cli.py`, `ui/tui.py`

Queue messages to fire at a specific time or after a delay — useful for net
windows, propagation forecasts, and low-power deferred sends. Backed by a new
`scheduled_messages` SQLite table so schedules survive app restarts.

**Delivered:**
1. ✅ **`scheduled_messages` table** in `store.py` — `ScheduledEntry` dataclass; `schedule_add`, `schedule_pending`, `schedule_cancel`, `schedule_mark_sent` methods.
2. ✅ **CLI** — `radioapp schedule add --delay 30m|1h --to CALL|--group EMS "text"` and `--at HH:MM|ISO`. `radioapp schedule list` / `radioapp schedule cancel <id>`.
3. ✅ **TUI** — `_check_scheduled` worker fires every 30 seconds via `set_interval`; `/sched +30m [text]` or `/sched HH:MM [text]` schedules from the composer or inline text.

---

## Offline band-plan / EmComm frequency reference

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/bandplan.py`, `cli.py`, `ui/tui.py`

An offline, no-dependency frequency reference covering JS8Call calling
frequencies (11 bands), common US EmComm/ARES/RACES simplex and net frequencies,
and select Winlink P2P/RMS spot frequencies.

**Delivered:**
1. ✅ **`core/bandplan.py`** — `FrequencyEntry` frozen dataclass + `ALL_ENTRIES` static table + `lookup(band, mode, region, transport)` with AND-combined filters + `format_mhz()` helper. Also `band_for_freq(hz) → str | None` (maps a dial frequency in Hz to a band name, e.g. `14_078_000 → "20m"`).
2. ✅ **CLI** — `radioapp bands [--band 40m] [--mode JS8] [--region US|INTL] [--transport winlink]`.
3. ✅ **TUI** — `/bands [band]` prints a formatted table to the log pane.
4. ✅ **Band tracking** — JS8Call and WSJT-X transports stamp `metadata["band"]` on every inbound message using `band_for_freq()`. `store.band_stats([transport, since])` returns per-band message counts. `radioapp history --band 40m` filters history; `radioapp bands --stats [--transport] [--since]` shows aggregate counts. The Health panel shows a compact `band log:` line under JS8Call rig state. HF messages in the TUI show a dim band tag (`20m`) between the transport badge and sender name.

---

## Host battery / power awareness

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/power.py`, `ui/tui.py`, `config.py`

Surface host battery state on the Health board — critical for field/portable
operation on a laptop or Pi with a UPS HAT.

**Delivered:**
1. ✅ **`core/power.py`** — `BatteryReading` dataclass + `read_battery()` reading `/sys/class/power_supply/` sysfs (no `psutil` dep). Reads `capacity`, `status`, `energy_now/power_now` (with `charge_now/current_now` fallback for drivers that report charge instead of energy). Fails gracefully when no battery is present.
2. ✅ **Health board** — battery percent + charging state + estimated runtime shown with threshold colouring (`warn_threshold` from `[power]` config, default 20%).

---

## Time consensus — multi-source UTC clock (GPS/chrony/NTP)

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/timesource.py`, `ui/tui.py`, `cli.py`

Accurate UTC is critical for HF digital modes (JS8Call uses strict 15-second
framing windows). Instead of a single internet-NTP check, the app now tries
time sources in priority order and reports the best available offset.

**Priority chain:** GPS (gpsd) → local chrony/ntpd daemon → internet NTP → system clock.

Radio_App **never starts or manages** gpsd or chrony — it only polls daemons
already running on the host.

**Delivered:**
1. ✅ **`query_gpsd_time(host, port, timeout)`** — connects to an already-running gpsd instance, reads the first TPV object with mode ≥ 2, extracts the GPS-disciplined `time` field (±100 ns from atomic clock), and returns the local clock offset in ms.
2. ✅ **`query_chronyc(timeout)`** — runs `chronyc tracking` as a subprocess (≈20 ms), parses the `System time: X seconds fast/slow` line. Works grid-down if chrony was previously GPS-disciplined and is in holdover.
3. ✅ **`query_ntpd(timeout)`** — runs `ntpq -c rv`, parses the `offset=` field (ms). Fallback when chronyc is not installed.
4. ✅ **`TimeConsensus` class** — `best_reading() -> TimeReading` tries sources in order, returns immediately on first success. `skip_gps/skip_local_ntp/skip_internet_ntp` flags for testing and explicit overrides. `TimeSourceKind` extended with `GPS` and `LOCAL_NTP` variants.
5. ✅ **Health board** — time line now shows source label: `GPS`, `local NTP`, `NTP`, or `system`; colour-coded offset (green < 100 ms / yellow < 1 s / red ≥ 1 s). Refreshed every 60 s inside `_refresh_health`.
6. ✅ **`radioapp time`** — prints best available offset and which source was used.

---

## Position beacon + GPS (Maidenhead grid)

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/position.py`, `transports/base.py`, `transports/js8call_transport.py`, `cli.py`, `ui/tui.py`, `config.py`

Situational awareness: show the station's position and grid square; send grid
beacons on supporting transports. JS8Call includes the grid square in its
transmissions once set; this wires that up automatically.

**Delivered:**
1. ✅ **`core/position.py`** — `Position` dataclass (auto-computes Maidenhead grid), `lat_lon_to_grid(lat, lon, precision=4|6)` pure WGS-84 converter, `GPSReader` (gpsd JSON socket API, returns `Position` with mode ≥ 2), `position_from_config(cfg)`.
2. ✅ **`TransportCapabilities.supports_position`** — new flag; JS8Call sets it `True`.
3. ✅ **JS8Call `send_position_beacon(position)`** — calls `STATION.SET_GRID` via the TCP/JSON API.
4. ✅ **Health board** — shows lat/lon/grid when position is known (config or GPS cache).
5. ✅ **CLI** — `radioapp position [--gps] [--beacon]`.

---

## Canned / template messages

**Requested:** 2026-06-27
**Status:** Done
**Area:** `core/templates.py`, `cli.py`, `ui/tui.py`, `config.py`

Quick-send pre-canned phrases without retyping them. Configured in `[templates]`
in `config.toml` as `name = "text"` pairs.

**Delivered:**
1. ✅ **`core/templates.py`** — `Templates` class loading from `[templates]` config section.
2. ✅ **CLI** — `radioapp templates` (list) / `radioapp templates send <name> --to CALL|--group EMS`.
3. ✅ **TUI** — `/tmpl [<name>]` lists templates or loads one into the composer for review before sending.

---

## Message export

**Requested:** 2026-06-26
**Status:** Done
**Area:** `cli.py`, `core/store.py`

Export conversation history to standard formats for archiving, handoff, or
post-incident review.

**Delivered:**
1. ✅ **`radioapp export --thread <key>|--all --format txt|json|md|maildir [--out path]`**
   - `txt` — one line per message with timestamp, sender, content; `--all` annotates thread
   - `json` — JSON array of `to_dict()` objects
   - `md` — `# thread` heading, `## YYYY-MM-DD` date groups, bold sender
   - `maildir` — RFC-2822 messages in `new/cur/tmp/` with `X-RadioApp-Transport/Thread/Status` headers; `--out` required
2. ✅ **Richer `store.query()` filters** — `status: str | None` (exact match) and `snr_min: float | None` (JSON metadata extract); surfaced as `--status`, `--snr-min`, `--date` on both `history` and `search`.

---

## Cross-mode group aggregation (incoming) — operator-declared membership

**Requested:** 2026-06-26
**Status:** Partially done (read-side / incoming built; outbound fan-out deferred)
**Area:** `core/groups.py`, `core/router.py`, `core/message.py`, `cli.py`,
`config.example.toml`

Aggregate one collective's traffic across every transport under a single label,
with membership **declared by the operator** (no automatic identity
reconciliation): "callsign XYZ is in EMS, mesh hash … is in EMS, the Winlink tag
`ems` is EMS" → all stamped as group `ems`.

**Delivered (incoming aggregation):**
1. ✅ **Membership model** — `Group` gained `members` (a list of
   `transport:identifier` specs; a bare identifier matches any transport, hex
   hashes are prefix-tolerant, callsigns case-insensitive) and `tags` (native
   group names / Winlink subject tags that map a message in regardless of
   sender). New `GroupMember` dataclass owns the matching.
2. ✅ **Reverse lookup** — `GroupRegistry.groups_for_message(msg)` resolves a
   message to its group(s) by sender membership and by tag (native `group` field
   + `metadata['tag']`), deduped and pure.
3. ✅ **Router stamping** — every inbound message is stamped with
   `metadata['groups']` (surfaced via `UnifiedMessage.groups`) so the store and
   UI can aggregate without re-deriving. Stamping can never break delivery.
4. ✅ **CLI** — `radioapp group <name> add|remove <transport:id>`,
   `tag|untag <tag>`, `show`, `delete`; `radioapp groups` now shows member/tag
   counts. Persisted to `[groups.<name>]` in the config TOML (round-trips,
   covered by backup/restore). Documented in `config.example.toml`.

**Deferred (separate request):**
- **Outbound fan-out** — "post to EMS across all member transports" with
  per-mode size caps + HF-encryption compliance + self-echo dedup.

**Follow-up done — Watch group filter (instead of a new panel):** rather than
build a separate group panel (which would largely duplicate Watch + the
favorites filter), the Watch stream gained a **group filter** alongside F4
favorites. The `[g]` key / "◯ Group" button cycles off → each configured group →
off; favorites and group filters are mutually exclusive. Reuses all of Watch's
rendering / pause / sort-by-mode machinery and is driven by the same
`metadata['groups']` stamp. A group also auto-claims its own name as a tag (a
JS8 `@EMS` message maps to group `EMS` with no extra config).

**Presentation sketch (not built — design only):** a single scrollback that
interleaves every member's messages in time order, each row prefixed with a
compact mode glyph (`[JS8] [Mesh] [RNS] [WL]`) + sender, so provenance stays
visible while the conversation reads as one. Mixed addressing is the catch — a
JS8 net call, a direct LXMF reply, and a Winlink email aren't the same
"conversation", so the pane is really a *filtered activity feed for the group*,
not a chat thread. Wrinkles to handle: per-mode latency means timestamps arrive
out of order (Winlink can land hours late — sort by received time and show the
origin time); width/format differ wildly (a 383-byte RNS note vs. a long email —
collapse long bodies with an expander); and replies have no cross-mode threading,
so visually group by mode-run or just stamp each row with its mode + reachability
dot. A right-hand member roster (who's in the group, per transport, last-heard)
would anchor it.

## Data hygiene: DB maintenance CLI + system health + backup/restore

**Requested:** 2026-06-26
**Status:** Done
**Area:** `core/store.py`, `core/nomad_cache.py`, `core/syshealth.py`,
`core/backup.py`, `cli.py`, `ui/tui.py`, `pyproject.toml`,
`config.example.toml`

Keep the local SQLite store from silently bloating small/offline devices, give
the operator visibility into host + database health, and make config + data
recoverable. Maintenance is **CLI-only** by design — these are deliberate,
sometimes destructive actions, not one-tap TUI buttons.

**Delivered:**
1. ✅ **DB maintenance CLI** — `radioapp db stats` (db size, message/thread
   counts, oldest/newest, cached-page count), `db vacuum` (reclaims free pages,
   reports bytes freed), `db prune --days N` (age-based message pruning),
   `db cache-prune --days N` / `db cache-clear` (NomadNet page-cache hygiene).
   Backed by `MessageStore.stats()` / `vacuum()` and
   `NomadPageCache.stats()` / `prune()` / `clear()`.
2. ✅ **Setup retention prompt** — `radioapp setup` now asks for history
   retention in days (0 = keep forever) with a note that the DB grows over time,
   persisted as `general.history_retention_days`. Documented in
   `config.example.toml`.
3. ✅ **System health on the Health board** — `core/syshealth.py` collects CPU /
   load, memory, temperature, and free-disk best-effort (stdlib `/proc`,
   `/sys/class/thermal`, `os.getloadavg`, `shutil.disk_usage`; optional `psutil`
   via the new `health` extra). The TUI Health screen renders a **System**
   section with threshold coloring plus a `data:` line (db size, msg/thread
   counts, cached pages, retention setting).
4. ✅ **Backup / restore CLI** — `core/backup.py` writes a `tar.gz` of the config
   file + a crash-consistent SQLite snapshot (online `Connection.backup()` API)
   with a manifest. `radioapp backup [--out path]` and
   `radioapp restore <archive> [--yes]`; restore writes `*.pre-restore-<ts>`
   safety copies and guards against path traversal in the archive.

## Interactive Winlink form composer (CLI/TUI)

**Requested:** 2026-06-26
**Status:** Done (CLI + TUI)
**Area:** `cli.py`, `ui/tui.py`, `transports/winlink_transport.py`

The Winlink-forms building blocks exist and are tested — `list_forms()`,
`update_forms()`, `get_form_template()` and `compose_form()` (which drives Pat's
browserless build → outbox flow) — but `compose_form()` had **no user entry
point**: a user could list and update forms but not actually fill in and send
one.

**Delivered (CLI):**
1. ✅ **`radioapp winlink form <template>`** — previews a template's processed
   text and best-effort auto-detects its prompt fields (`<Var>`/`<Ask>`/HTML
   `name=`/`{Brace}` shapes, via `detect_form_fields()`) so the operator knows
   which `--field NAME=VALUE` keys to fill.
2. ✅ **`radioapp winlink compose-form <template> [--field k=v ...]
   [--responses FILE] [--to/--cc/--subject ...]`** — merges `--responses` JSON +
   repeated `--field` (field wins on collision), calls `compose_form()`, and
   prints the built `{to, cc, subject, body}` for review.

**Delivered (TUI):**
3. ✅ **"📋 Forms" button** on the Winlink action bar drives the whole flow with
   `push_screen_wait`: a **`WinlinkFormsScreen`** picker (filterable list of the
   flattened catalog) → a **`WinlinkComposeFormScreen`** generated field form
   (one input per detected field + To/Cc/Subject overrides) → `compose_form()`,
   then a system-log review line. Field detection is shared with the CLI.

Both paths queue to **Pat's outbox** (a staging area) — nothing transmits until
`winlink connect`, which is the natural review gate.



## Offline NomadNet page cache + "sync favorites now"

**Requested:** 2026-06-26
**Status:** Done
**Area:** `core/nomadnet.py`, `core/nomad_cache.py`, `core/favorites.py`,
`ui/tui.py`, `cli.py`

Sync NomadNet pages locally so favorite nodes are viewable offline, with a
"sync favorites now" action to pull the latest once Reticulum is back up. The
pieces already exist: `NomadnetBrowser.fetch()` returns a `PageResult` with the
decoded micron `content` (a clean seam to cache), favorites already tag NomadNet
servers via `Favorite.kind == "node"`, the TUI NomadNet surface has node lists +
an F4 favorites filter + an `f`/"Save node" binding, and the Reticulum transport
now auto-reconnects (so a sync can fire when the stack comes online).

**Sketch of work:**
1. ✅ **Page cache** — `core/nomad_cache.py` (`NomadPageCache`) backs a
   `nomad_pages(dest, path, content, fetched_at, ok)` table (PK `(dest, path)`)
   in the existing SQLite DB.
2. ✅ **Wire caching into `fetch()`** — every successful (static) fetch upserts
   `content`; `prefer_cache`/offline serves the cached page (and a failed live
   fetch falls back to it), flagged with its `fetched_at` age. CLI `browse
   --offline`; TUI browser shows a "cached <age>" status. Dynamic (`field_data`)
   pages are intentionally not cached.
3. ✅ **`sync_favorites()`** — `NomadnetBrowser.sync_favorites()` iterates
   `kind == "node"` favorites, fetches each node's `/page/index.mu` live, caches
   the result, and returns a `FavoritesSyncResult(ok, failed, skipped, pages)`.
   `--follow-links` additionally mirrors same-node `/page/*.mu` links one level
   deep (off by default — expensive over LoRa). Offline → all skipped.
4. ✅ **Surfaces** — CLI `radioapp nomad sync [--follow-links] [--timeout]`; TUI
   NomadNet view has an `s`/"Sync favs" action + button that runs the sync in a
   worker and reports the tally.

**Caveats / scope decisions:**
- **Dynamic pages** (rendered from `field_data` `var=value`) only cache a single
  snapshot — label them as such; static `/page/*.mu` pages are the real win.
- **Link-following depth** — default to index-only (cheap); deeper mirroring
  multiplies link round-trips, expensive over LoRa, so make it opt-in.
- **Staleness** — cached views must visibly show their `fetched_at` age so a
  cached page is never mistaken for live.
- **No change-detection** — NomadNet can't signal that a page changed, so sync is
  a manual/periodic pull; "latest" means "as of the last sync."

## Past-chats history view + searchable message archive

**Requested:** 2026-06-26
**Status:** Done (CLI history + search, FTS5, TUI search palette, export all shipped; lazy scroll-back and "All chats" archive view remain as polish)
**Area:** `core/store.py`, `cli.py`, `ui/tui.py`

Give users a way to browse and search their full conversation history offline.
The foundation already exists: every message from every transport is normalized
to a `UnifiedMessage` and persisted in one SQLite table (`conversations.db` →
`messages`), with `sender, recipient, group, content, transport, status,
metadata, timestamp` indexed by `(thread_key, timestamp)`. This is a
read/query feature, not a data-model change.

**Possibilities (rough easiest → most involved):**
1. ✅ **CLI history/search** — `MessageStore.query(...)` (a flexible ANDed filter:
   thread/transport/sender/group/text + since/until + limit, newest-N selection
   with optional oldest-first read order). Surfaced as `radioapp history
   [--thread/--to] [--mode] [--from] [--group] [--since] [--until] [--limit]
   [--format text|json]` and `radioapp search "<text>" [same filters]`. Both open
   the store directly (no transports started) so they're instant and work
   offline; group/cross-mode lines are annotated with their thread + group tags.
2. **Full-text search (FTS5)** — add a SQLite FTS5 virtual table mirroring
   `content` (+ sender/group), synced via triggers/on-insert, for fast ranked
   queries with `snippet()` highlighting. FTS5 ships with Python's `sqlite3` (no
   new dependency). ✅ **Done** — external-content `messages_fts` (unicode61)
   kept in sync by AFTER INSERT/UPDATE/DELETE triggers, backfilled on first
   creation (migration via `'rebuild'`). New `MessageStore.search_ranked()`
   returns `SearchHit`s (message + highlighted snippet + thread) ordered by BM25
   `rank`, with a transparent LIKE fallback when a sqlite build lacks FTS5.
   Single words prefix-match; multi-word terms match as a phrase.
3. **TUI history/search surface** — a search box (`/search <text>` or a `Ctrl+F`
   palette) listing matches with mode + timestamp that jump into the thread at the
   hit; plus a cross-mode "All chats" archive view (the thread list scopes by
   transport — this is the superset) with mode/date/group/unread filters and
   lazy-loaded scroll-back for huge threads. ✅ **Search palette done** — a
   dedicated `#search-view` (Ctrl+F / `/search`) with a live, type-ahead input
   over `search_ranked()`; results show time · mode · sender · highlighted
   snippet, Enter opens the conversation (switching to its mode), Esc restores
   the prior surface. The cross-mode "All chats" archive view + lazy scroll-back
   remain a later polish item.
4. ✅ **Richer filters** — `--status`, `--snr-min`, `--date` on `history` and `search`; `store.query()` accepts `status` and `snr_min` params with SQL JSON extract for SNR.
5. ✅ **Band filter** — `radioapp history --band 40m` filters to HF messages on a specific band; `store.query(band=)` uses `json_extract(metadata, '$.band')`. Pairs with band tracking above.
6. ✅ **Export** — `radioapp export --thread <key>|--all --format txt|json|md|maildir [--out path]`.
7. **Retention/housekeeping** — optional age/size-based pruning, per-thread
   "keep forever" pins, and VACUUM so history doesn't bloat small devices.

**Suggested sequencing:** start with #1 (CLI history+search) over `MessageStore`,
add #2 (FTS5) under the same query API as volume grows, then surface it in the
TUI (#3).

## Reticulum file transfer (attachments)

**Requested:** 2026-06-26
**Status:** Done
**Area:** `transports/reticulum_transport.py`, `transports/base.py`,
`core/message.py`, `ui/tui.py`, `config.example.toml`

Add the ability to send files over Reticulum, the way `/attach` already works
for Winlink. Previously file attachment was hard-gated to Winlink and the
Reticulum `send()` path only forwarded `msg.content` (text) into the
`LXMF.LXMessage` — it never touched `metadata["attach"]`.

**Delivered:**
1. ✅ **Capability flag** — `TransportCapabilities.supports_attachments` (set on
   Winlink + Reticulum). The TUI's `/attach` affordance is now driven by this
   flag instead of a hardcoded transport name, so any future transport that can
   carry files gets it for free.
2. ✅ **Formalised attachment convention** — `UnifiedMessage` gained
   `attach_paths` / `attachment_names` / `saved_attachments` helpers over the
   shared `metadata["attach"]` (outbound paths), `metadata["attachments"]`
   (display names) and `metadata["attachments_saved"]` (inbound saved paths)
   keys used by both Winlink and Reticulum.
3. ✅ **Outbound** — `ReticulumTransport.send` reads the queued paths and
   populates `lxm.fields[LXMF.FIELD_FILE_ATTACHMENTS] = [[name, data], ...]`,
   forcing the `DIRECT`/Resource method so RNS chunks large payloads. Group/
   broadcast sends are DIRECT-only for files (a single 383-byte packet can't
   carry them) — the TUI keeps the queue and warns instead of dropping it.
4. ✅ **Inbound** — the LXMF delivery callback extracts file fields, saves them
   to a configurable directory (`[transports.reticulum] attachments_dir`,
   default `$XDG_DATA_HOME/radio_app/attachments`) with path-traversal-safe,
   de-duplicated names, and records names + saved paths on the message so the
   UI shows the paperclip + filenames.
5. ✅ **TUI** — `/attach` un-gated via the capability flag (renamed the internal
   queue to `_attach_queue`); injection in `_send` is now capability-driven and
   direct-only.

**Notes:** Group/broadcast Reticulum sends remain text-only (single packet
capped at 383 bytes, `_GROUP_PAYLOAD_MAX`), so file transfer is DIRECT-only.

---

## WSJT-X transport

**Requested:** 2026-06-27
**Status:** Done
**Area:** `transports/wsjt_x_transport.py`, `ui/tui.py`

✅ Shipped. FT8/FT4 weak-signal mode via WSJT-X's UDP API (port 2237).

**What shipped:**
- UDP listener for `Decode`, `Status`, `Heartbeat`, `QSOLogged`, `Clear`
  datagrams (binary `QDataStream`-encoded protocol).
- `Decode` → `UnifiedMessage` with `transport="wsjtx"`, kind `"decode"`.
- `FreeText` datagram outbound (13-char limit enforced; message truncated with
  warning if over).
- `Status` heartbeats populate the Health board (frequency, mode, DX call,
  TX/RX state, grid); callsign auto-learned from `de_call` if not configured.
- `TransportCapabilities`: `supports_addressing=True`,
  `supports_broadcast=True`, `supports_groups=False`,
  `max_content_bytes=13`.
- ⚡ Start button in the WSJT-X mode bar; `launch_cmd = "wsjtx"` default.
- 34 tests covering decode, status, outbound, health board, and ⚡ Start.

---

## Weather & Environmental Data

Four paths for pulling weather data over radio, ranked by practicality.
All four share a common `metadata["kind"] = "weather_bulletin"` convention so
the Watch live-feed and a future Weather tab can filter across transports uniformly.

---

### JS8Call — APRS weather relay

**Requested:** 2026-06-28
**Status:** Backlog
**Area:** `transports/js8call_transport.py`, `ui/tui.py`

JS8Call exposes an `@APRSIS` gateway that routes APRS messages onto APRS-IS
(the internet APRS backbone). NWS weather queries can be sent through it: the
operator sends a directed message to `@APRSIS` containing a standard APRS
weather-query packet, and the NWS point forecast comes back as a normal directed
JS8 message from the relay. Works entirely over HF RF — the relay node handles
the internet leg.

**Design notes:**
- A `/wx [grid]` command composes and sends the APRS query; grid square
  auto-populated from `[station].grid_square` or the most recently heard
  `GRID:` heartbeat.
- Response arrives as `RX.DIRECTED`; detect by sender/command pattern and stamp
  `metadata["kind"] = "weather_bulletin"` so it surfaces distinctly.
- Grid square metadata already parsed by the transport (`GRID:` fields in
  `RX.DIRECTED` params) — useful for contextualising whose observation is whose.
- The existing Watch live-feed is the natural rendering surface.

**UX sketch:**
```
/wx              → NWS point forecast for your configured grid square
/wx EM72         → explicit grid override
→ pinned message: "NWS EM72 — Tonight: Partly cloudy. Low 64°F. SE wind 8 mph."
```

---

### Winlink — NWS bulletin detection

**Requested:** 2026-06-28
**Status:** Backlog
**Area:** `transports/winlink_transport.py`, `ui/tui.py`

Winlink is often the only internet-connected path in a grid-down deployment.
NWS provides free email alert subscriptions (forecast.weather.gov); bulletins
arrive in the Pat inbox as regular email with recognisable subject patterns
(`NWS-BULLETIN`, `FPUS51 KWNO`, `MARINE FORECAST`, `CONVECTIVE OUTLOOK`, etc.).
Detecting these and surfacing them separately from operator email is the whole
feature — no new protocol work.

**Design notes:**
- Subject-line pattern match on ingest; stamp `metadata["kind"] =
  "weather_bulletin"` and optionally `metadata["nws_office"]` from the WMO
  header (e.g. `KWNO`, `KBOX`).
- Works over any Winlink connect path: telnet, VARA HF, ARDOP, Pactor, AX.25.
- Secondary: adapt the ICS-213 form template as a **weather observation form**
  (fields: temp, wind speed/direction, sky cover, remarks) for operator-sourced
  field reports.
- GRIB or CSV attachments already saved by the transport; a co-located tool
  could render them later.

**UX sketch:**
```
Winlink inbox → [Weather] tab  (filtered by metadata kind)
NWS-BULLETIN  "MARINE FORECAST CAPE COD BAY"   2026-06-28 06:15
FPUS51 KBOX   "AREA FORECAST DISCUSSION"        2026-06-28 05:44
```

---

### Reticulum — #weather group broadcast

**Requested:** 2026-06-28
**Status:** Backlog
**Area:** `transports/reticulum_transport.py`, `ui/tui.py`, `config.example.toml`

A Reticulum GROUP destination (symmetric key derived from group name) lets a
single internet-connected mesh node pull NWS feeds and re-broadcast them
encrypted to all local RNS nodes over LoRa — no internet required at the
receiving end. The well-known group names `#weather` and `#nws_alerts` become
the convention so deployments interoperate without coordination.

**Design notes:**
- GROUP destinations already used for cross-mode group aggregation; no new
  protocol primitives needed.
- LXMF file attachments (JSON/CSV/GRIB) already saved by the transport; full
  NWS text forecasts fit easily within the 1 MB LXMF payload limit.
- `#nws_alerts` as an urgent-only sub-group is worth documenting as a convention
  alongside `#weather`.
- Stamp `metadata["kind"] = "weather_bulletin"` on receipt for uniform Watch
  filtering.

**UX sketch:**
```toml
# config.toml — join the well-known weather groups
[groups.WX]
display_name = "Weather"
transports   = ["reticulum"]
tags         = ["weather", "nws_alerts"]
```

---

### MeshCore — #weather channel convention

**Requested:** 2026-06-28
**Status:** Deferred — usage pattern only, no code change needed
**Area:** `transports/meshcore_transport.py`

MeshCore channels carry plaintext up to 134 bytes/frame. A `#weather` hashtag
channel for operator-entered observations works today with no app changes.
Document as a usage convention in operator guides rather than a distinct feature.

**Design notes:**
- Convention: sender prepends `"WX: "` to observation text (e.g. `"Node1: WX:
  18C, 15kt E, partly cloudy"`).
- Revisit if a structured sensor-data frame standard emerges in MeshCore
  firmware (e.g. BME280 nodes broadcasting temp/humidity/pressure in a parseable
  format); at that point parse into `metadata["kind"] = "weather_observation"`
  and surface on the Health board.
- WSJT-X excluded: 13-char FT8 limit makes weather payloads impractical.

