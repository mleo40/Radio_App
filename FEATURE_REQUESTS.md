# Feature Request Queue

Tracked, not-yet-implemented feature requests. Newest at the top.

## Cross-mode group aggregation (incoming) — operator-declared membership

**Requested:** 2026-06-26
**Status:** Partially done (read-side / incoming built; outbound fan-out deferred)
**Area:** `core/groups.py`, `core/router.py`, `core/message.py`, `cli.py`,
`config.example.toml`

Aggregate one collective's traffic across every transport under a single label,
with membership **declared by the operator** (no automatic identity
reconciliation): "callsign XYZ is in TTP, mesh hash … is in TTP, the Winlink tag
`ttp` is TTP" → all stamped as group `ttp`.

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
- **Outbound fan-out** — "post to TTP across all member transports" with
  per-mode size caps + HF-encryption compliance + self-echo dedup.

**Follow-up done — Watch group filter (instead of a new panel):** rather than
build a separate group panel (which would largely duplicate Watch + the
favorites filter), the Watch stream gained a **group filter** alongside F4
favorites. The `[g]` key / "◯ Group" button cycles off → each configured group →
off; favorites and group filters are mutually exclusive. Reuses all of Watch's
rendering / pause / sort-by-mode machinery and is driven by the same
`metadata['groups']` stamp. A group also auto-claims its own name as a tag (a
JS8 `@TTP` message maps to group `TTP` with no extra config).

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
**Status:** In progress (CLI history + search done; FTS5 + TUI surface queued)
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
   new dependency).
3. **TUI history/search surface** — a search box (`/search <text>` or a `Ctrl+F`
   palette) listing matches with mode + timestamp that jump into the thread at the
   hit; plus a cross-mode "All chats" archive view (the thread list scopes by
   transport — this is the superset) with mode/date/group/unread filters and
   lazy-loaded scroll-back for huge threads.
4. **Richer filters** — query by captured `metadata` (SNR, hops, frequency,
   delivery status), date-bucketed "jump to date", per-contact stats.
5. **Export/backup** — `radioapp export --thread <key> --format {json,txt,md,
   maildir}` and a global `export --all`.
6. **Retention/housekeeping** — optional age/size-based pruning, per-thread
   "keep forever" pins, and VACUUM so history doesn't bloat small devices.

**Suggested sequencing:** start with #1 (CLI history+search) over `MessageStore`,
add #2 (FTS5) under the same query API as volume grows, then surface it in the
TUI (#3).

## Reticulum file transfer (attachments)

**Requested:** 2026-06-26
**Status:** Queued
**Area:** `transports/reticulum_transport.py`, `core/message.py`, `ui/tui.py`

Add the ability to send files over Reticulum, the way `/attach` already works
for Winlink. Today file attachment is hard-gated to Winlink
(`tui.py` ~L3916: `if self.active_transport == "winlink" and self._winlink_attach`)
and the Reticulum `send()` path only forwards `msg.content` (text) into the
`LXMF.LXMessage` — it never touches `metadata["attach"]`.

LXMF/RNS natively support this (LXMF `fields` + Resource-based chunked transfer),
and `capabilities()` already advertises `max_message_size=1_000_000`, so the
plumbing exists but is unwired.

**Sketch of work:**
1. Give `UnifiedMessage` a structured attachment representation (or formalize the
   existing `metadata["attach"]` convention).
2. In `ReticulumTransport.send`, read the queued paths and populate
   `lxm.fields[LXMF.FIELD_FILE_ATTACHMENTS] = [[name, data], ...]`, forcing the
   `DIRECT`/Resource method for large payloads (RNS handles chunked transfer).
3. Handle inbound `fields` attachments in the LXMF delivery callback (save +
   surface them in the UI).
4. Un-gate `/attach` in the TUI so it isn't Winlink-only — ideally drive it from a
   new `supports_attachments` capability flag instead of a hardcoded transport
   name.

**Notes:** Group/broadcast Reticulum sends are a single packet capped at 383 bytes
(`_GROUP_PAYLOAD_MAX`), so file transfer would be DIRECT-only initially.

