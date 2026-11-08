# Configuration

Behaviour is set by environment variables, the `--port` option of `token-burn serve`, constants in `collector/util.py`, browser preferences and two small files: `location.json` (where the data folder is) and `config.json` (in the data folder: install and sync settings).

## Port

| Setting | Default | How to change |
|---|---|---|
| Dashboard port | 8421 | `TOKEN_BURN_PORT=<n>` or `token-burn serve --port <n>` by hand (the scheduled server job and install health check always use the default) |

The server binds to `127.0.0.1` only. A non-loopback client gets 403, and requests with an unexpected `Host` header are rejected.

## Environment variables

| Variable | Effect | Notes |
|---|---|---|
| `TOKEN_BURN_DATA_DIR` | Replaces the data directory (database, `usage.json`, logs, hook logs, `config.json`). Takes priority over `location.json`. While it is set the data folder is fixed: `token-burn install --data-dir` and `token-burn data move` refuse with exit code 3, and `uninstall` leaves `location.json` alone | Read once at start-up. Useful for testing two computers on one PC (see [SYNC](SYNC.md)). Honoured by the collector, server and both hooks. To make it permanent set it as a user environment variable, then re-run `token-burn install` so scheduled jobs pick it up, and restart Cursor |
| `TOKEN_BURN_PORT` | Dashboard port | Overridden by `--port` |
| `TOKEN_BURN_CLAUDE_TOKEN` | Claude OAuth access token used for plan lookups | Highest priority in the credential resolver. Falls back to the optional `keyring` extra, then Claude's own credentials file or macOS Keychain |
| `CURSOR_STATE_DB` | Path to Cursor's `state.vscdb` | Overrides the per-OS default location |
| `TOKEN_BURN_OPENCODE_DB` | Path to opencode's `opencode.db` | Overrides the default location below |
| `XDG_DATA_HOME` | Base folder for opencode's data | Honoured on every OS when `TOKEN_BURN_OPENCODE_DB` is not set |
| `CLAUDE_CONFIG_DIR` | Claude's folder (projects, credentials, settings) instead of `~/.claude` | Same variable Claude Code uses. `token-burn install` honours it when registering the hook |

## Data directory by OS

| OS | Default |
|---|---|
| Windows | `%LOCALAPPDATA%\token-burn\data` |
| macOS | `~/Library/Application Support/token-burn/data` |
| Linux | `$XDG_STATE_HOME/token-burn` or `~/.local/state/token-burn` |

### Choosing the data folder, `location.json`

The install wizard lets you choose the data folder. So that every process (scheduled tasks, the server, the Claude and Cursor hooks and the command line) finds a custom folder, the choice is recorded in a pointer file at a fixed per-OS path. It is written only when you pick a non-default folder and removed when you go back to the default.

| OS | Pointer file |
|---|---|
| Windows | `%LOCALAPPDATA%\token-burn\location.json` |
| macOS | `~/Library/Application Support/token-burn/location.json` |
| Linux | `$XDG_CONFIG_HOME/token-burn/location.json` or `~/.config/token-burn/location.json` |

```json
{"version": 1, "data_dir": "D:\\TokenBurnData"}
```

Resolution order for the data directory: the `TOKEN_BURN_DATA_DIR` environment variable, then `location.json` (when valid, absolute, and existing or creatable), then the per-OS default above. An invalid pointer is ignored and the default is used; `token-burn status` reports it.

Rules for a chosen data folder: writable, not a temporary folder, not inside a synced folder (OneDrive, Dropbox, iCloud Drive, Google Drive: the live SQLite database can be corrupted by sync), not a network or UNC path, not inside the program folder, and either empty or already holding Token Burn data (a `usage.db`, or a `config.json` that is Token Burn's own: it has `version` and `app_version`, or a `sync` block). A folder with other files in it is refused. Installing into a folder that already holds this computer's `config.json` reuses its machine id. To combine computers, use the shared folder instead ([SYNC](SYNC.md)).

```
token-burn data status          # shows the data folder, how it was chosen and its size
token-burn data move <folder>   # stops tasks and server, copies usage.db (SQLite backup API), config.json,
                                # usage.json, logs, adopted-*.json.gz and the peer cache, verifies the copy
                                # (read-only open, row counts per table), writes location.json, restarts,
                                # then asks before deleting the old copy
token-burn data move <folder> --yes   # no questions; also deletes the old copy
```

`data move` refuses (exit 3) a target that already holds `usage.db`, a target inside the current folder or the other way round, a folder that fails the rules above, and any move while `TOKEN_BURN_DATA_DIR` is set. If the copy cannot be verified the old data is left untouched and the tasks are restarted (exit 1). Without `--yes` and without a terminal the old copy is kept.

### `config.json` (in the data folder)

Written by the installer and `token-burn sync` commands (atomically, mode 0600 on macOS and Linux). A missing file, or `"sync": {"enabled": false}`, means sync is off, and then `usage.json` has no `sync` or `machines` keys at all. A `config.json` created only by `token-burn sync enable` (without the installer) may hold just the `sync` block.

```json
{"version": 1, "app_version": "0.2.0",
 "install_dir": "C:\\Users\\me\\AppData\\Local\\Programs\\Token Burn",
 "data_dir": "C:\\Users\\me\\AppData\\Local\\token-burn\\data",
 "sync": {"enabled": true, "folder": "C:\\Users\\me\\OneDrive",
          "machine_id": "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7", "machine_name": "Office PC",
          "joined_at": "2026-11-05T03:00:00Z", "adopted": false}}
```

| Key | Meaning |
|---|---|
| `version` | Schema version of this file. It stays 1 and does not follow the program version |
| `app_version` | The Token Burn version that last ran the installer (for example `0.2.0`). Used for the "Upgrade it to ...?" question. Not the schema version |
| `install_dir` | Program folder (zip installs); used for upgrades and uninstall |
| `data_dir` | Recorded for display only; `location.json` and the environment decide where data lives |
| `sync.enabled` | Whether this computer shares usage |
| `sync.folder` | The synced folder you chose (the app creates `token-burn` inside it; if you choose a folder called `token-burn` its parent is used) |
| `sync.machine_id` | 32 hex characters, random, the only thing used to build file names. Kept across reinstalls, removed by `uninstall --purge-data` |
| `sync.machine_name` | Display name, 1 to 40 characters, unique among computers |
| `sync.joined_at` | When this computer joined |
| `sync.adopted` | True when this computer took over an existing identity after a reinstall |

Sync state lives in the database's key-value table: `sync:last_hash`, `sync:last_write`, `sync:last_token` and `sync:conflict`.

### Sync settings and commands

Constants (in `collector/sync/`, not user-editable): write throttle 5 minutes (300 s), heartbeat 30 minutes (1800 s, `HEARTBEAT_S`), a computer is stale after 1 hour, at most 64 computers are read per pass. See [SYNC](SYNC.md) for the full guide.

```
token-burn sync status
token-burn sync enable [--folder PATH] [--new | --join] [--machine-name NAME] [--yes]
token-burn sync disable [--remove-my-data] [--yes]
token-burn sync machines
token-burn sync rename NEW_NAME [--machine NAME]
token-burn sync retire NAME | unretire NAME
token-burn sync remove NAME [--yes]
token-burn sync reset-id [--yes]
token-burn sync now
```

Names are matched case-insensitively; an ambiguous name lists the candidates and an id (8 to 32 hex characters) is accepted. `remove`, `disable --remove-my-data` and `reset-id` are refused with exit 3 when there is no terminal and no `--yes`. Exit codes: 0 ok, 1 error (including a name or id that matches no computer), 2 bad arguments, 3 refused or cancelled.

## Cursor database location

| OS | Default |
|---|---|
| Windows | `%APPDATA%\Cursor\User\globalStorage\state.vscdb` |
| macOS | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` |
| Linux | `~/.config/Cursor/User/globalStorage/state.vscdb` |

Set `CURSOR_STATE_DB` to override.

## opencode database location

opencode's data is a read-only SQLite file at `~/.local/share/opencode/opencode.db` on every OS (including Windows and macOS). If `XDG_DATA_HOME` is set, the file is `$XDG_DATA_HOME/opencode/opencode.db`. Set `TOKEN_BURN_OPENCODE_DB` to override both. opencode counts as installed once this file exists.

Only the v2 layout (`session_v2` and `session_message` tables) is supported; any other layout shows the status "unsupported opencode data format". The file is opened read-only and never written, and opencode's credential tables are never read. Per request, the model label is `provider/model`, the effort is the model variant, reasoning tokens are added to output, compaction rows are included, and a child session counts as a subagent. opencode has no plan quota, so no plan lookup is made.

Cost: opencode records cost 0 for free or gateway models, so cost can read US$0.00. The page shows a note when any model with token use reported no cost (`meta.cost_reported` is false). Token use is the figure to rely on. Totals match opencode's own session totals to within about 0.4 percent (a known gap, probably rows still in progress, which are counted only once they complete, or after 24 hours if abandoned).

## Claude plan credentials

Plan lookups resolve a token in this order: `TOKEN_BURN_CLAUDE_TOKEN`, the system keyring (only if the `keyring` extra is installed), Claude's `.credentials.json` in the Claude config folder, then on macOS the Keychain item "Claude Code-credentials" (macOS may prompt once to allow access; choose Always Allow for the `token-burn` or Python binary). Credentials are only read, never written.

## Constants

Defined in `collector/util.py` (and listed in `docs/DATA-CONTRACT.md`).

| Constant | Value | Meaning |
|---|---|---|
| `TOKEN_REFRESH_S` | 60 s | Collector schedule and page poll interval |
| `PLAN_INTERVAL_S` | 590 s | Plan lookup throttle per platform (about 10 minutes, less slack for task start jitter) |
| `MINUTES` | 43,200 | 30 days of 1-minute buckets; also the retention |
| `DEFAULT_PORT` | 8421 | Server default |
| `LOCK_STALE_S` | 300 s | A collect lock older than this is taken over |
| `LOG_MAX` | 1 MB | Log size before it rotates to `.1` |

Cursor adapter constants (`collector/adapters/cursor.py`): page size 100, at most 100 pages per fetch, incremental overlap of 2 hours, window of 30 days.

If you change a constant, the page's countdown and note text follow from `usage.json` (`token_refresh_s`, `plan_refresh_s`), but the scheduler interval is set by `token-burn install` (re-run it after changing the project).

## Windows

The page offers 7 windows. The default is 12 hours.

| Key | Label | Minutes |
|---|---|---|
| 30m | 30 min | 30 |
| 1h | 1 hour | 60 |
| 6h | 6 hours | 360 |
| 12h | 12 hours | 720 (default) |
| 24h | 24 hours | 1,440 |
| 7d | 7 days | 10,080 |
| 30d | 30 days | 43,200 |

A window is anchored to the browser clock: `endIdx = floor((now - start) / 60000)`, `loIdx = endIdx - W + 1`. One window selector drives all visible columns.

## Usage graph modes

Each platform column (Claude, Cursor and opencode) has its own graph mode switch. **Cumulative** is the default and draws running totals across the selected window. **Rolling total** draws, for each point in the window, the usage over the preceding period, one line per model and effort plus a bold Total line (not a selectable chip). The "Total over the last" picker offers 30 min, 1 hour, 6 hours, 12 hours, 24 hours (default), 7 days and 30 days, and is hidden in Cumulative mode. Switching mode or period redraws from the data already loaded, with no refetch. Clicking a model chip (or its chart line) highlights that series and shows a small callout over the chart with its usage for the selected main time period (tokens or US$ following the metric toggle), plus, in Rolling total mode, the latest rolling total. Clicking again clears it.

History is limited to 30 days. When the window reaches back before the first stored data, the earliest points sum less than a full period, and a note under the chart gives the date and time history starts.

## Sessions card

Under each Usage graph, the **Sessions** card draws one horizontal bar per session with usage in the selected window, split into segments by model and effort in the same colours as the graph. It follows the window selector, the Tokens/Cost switch and the computer filter. The 15 largest sessions get their own bar and the rest are summed into "Other (N sessions)"; the table below lists every session with its models, tokens, cost and last activity. Clicking a segment or a model button selects that model, as clicking its chip does.

Session names: Claude uses the automatic session title, or the name given with `/rename`, which always wins. opencode uses its session title. Cursor uses the chat name stored by this computer's Cursor app (looked up at most once a day per conversation); conversations from other computers, background agents and deleted chats show a short ID. With sync on, titles are shared with your other computers in the machine files. Prompt text is never read or shared.

The Claude and opencode columns also have an **Agents** switch beside the graph mode. **All** (default) draws one line per model and effort; **Split** draws two, solid for the primary agent and dashed for subagents, in the same colour, in both graph modes. The Rolling total line stays the total of all usage, and Cursor has no switch because it reports no subagent tokens. The opencode column's summary row also shows Primary agent, Subagents and Cache hit rate tiles. The Claude column's summary row also shows Primary agent, Subagents and Cache hit rate tiles for the selected window; Cache hit rate is cache read divided by (input + cache read + cache write), with the cache read tokens underneath, and is hidden when usage.json has no `cache_read` data. The Cursor column's summary row also shows a Cache hit rate tile, calculated the same way. Every column with token counts also shows an **Input : output** tile: input-side tokens (fresh input plus cache read and write) divided by output tokens, with both counts underneath and the full figures on hover.

Each column has a **Value and limits** card. **Value for money** shows the cost of the last 30 days (or the history there is) at that platform's prices: API list prices for Claude, what Cursor charged for Cursor, and what opencode reported for opencode. Type your monthly plan fee in US$ and it shows how many times the fee that usage would have cost per token, with the fee pro-rated to the same number of days. The fee is kept in this browser only. **Limit forecast** projects each plan pool to 100% from its recent pace (the last hour for the 5-hour pool, the last 24 hours for the others, counting only samples since the last reset) and says whether it will hit the limit before it resets, or what it will be at when it resets. It needs at least 10 minutes of samples.

## localStorage keys

Per browser, per origin, wrapped in try/catch so a blocked store never breaks the page.

| Key | Values | Meaning |
|---|---|---|
| `token-burn.window` | one of the window keys | Remembered window |
| `token-burn.metric` | `tokens` or `cost` | Remembered Tokens/Cost toggle |
| `token-burn.agents.<platform>` | `all` or `split` | Claude and opencode usage graph primary/subagent split |
| `token-burn.graph.<platform>` | `cumulative` or `rolling` | Usage graph mode for `claude`, `cursor` or `opencode` |
| `token-burn.providers` | list of visible providers | Which columns the header toggle buttons show. Uninstalled providers are disabled |
| `token-burn.span.<platform>` | one of the span keys (`30m` to `30d`) | Rolling total period for that column |
| `token-burn.machines` | object of `{machineId: false}` | Computers switched off in the header toggle row (sync only). Unknown ids are ignored, and all off is treated as All |
| `token-burn.fee.<platform>` | a number of US$ | Monthly plan fee typed into that column's Value for money box. Removed when the box is cleared |

A different port is a different origin, so preferences do not carry across ports.

## Deliberate behaviour changes against the sources

| Area | Old Claude app | Old Cursor app | Combined app |
|---|---|---|---|
| Cursor plan lookup | n/a | Every minute | Every 590 s (about 10 minutes), through the same throttle as Claude. A failure retries next minute |
| Cursor token fetch | n/a | Whole 30 days every minute (up to 100 pages) | Full 30 days only on backfill or first run; afterwards from the newest event minus 2 hours, de-duplicated, usually one page |
| Cursor effort | n/a | Left inside the model name | Parsed from the model name into a separate effort column. The model is shown without the suffix, for example `grok-4.7` with effort `high` |
| Cursor window | n/a | From local midnight 29 days ago | A true rolling 30 days (43,200 minutes) like Claude |
| Cursor charts | n/a | Hand-drawn SVG | Chart.js, shared with Claude. The cumulative lines, hover tooltip and click-to-highlight are kept |
| Storage | `state.json` and `usage.json` | 5.8 MB dense snapshot | SQLite `usage.db` and a sparse `usage.json` |
| Token split | Discarded | Discarded | Input, output, cache read and cache write kept, shown as Input, Output and Total |
| Port | 8417 | 8765 | 8421 (the original app uses 8420; this app serves both on one port) |

### User interface changes

| Source | Change |
|---|---|
| Claude "Models in window" list | Replaced by the **Statistics** table plus model/effort **series chips** above the chart |
| Claude "Subagents by type" summary | Replaced by a per-run **Subagents** table (name, type, model, effort, input, output, total, cost, % of overall) |
| Claude `?embed=1` compact mode | Removed. There is no embed mode, and the desktop-app pane mod is not part of this project |
| Cursor SVG charts | Replaced by Chart.js, with LTTB decimation at 500 samples |
| Primary and Subagent usage columns | The **Primary usage** and **Subagent usage** values in the Statistics table follow the Tokens/Cost toggle |
| Tables | All tables are sortable, with a search box and model/effort filters. No pagination |

## Known limitations

- **Unofficial APIs.** The Anthropic `oauth/usage` endpoint and the Cursor dashboard and plan endpoints are not documented or supported, and can change without notice.
- **Cursor subagent tokens** are attributed only if Cursor's usage events carry the subagent's conversation id. Otherwise the Subagents table shows n/a for tokens and cost, and subagent usage is inside the primary figures.
- **Claude subagent cost** is the subagent's share of its session cost (by tokens), not a separate figure from Claude. Running sessions are estimated until Claude records a cost.
- **Token totals include cache tokens**, as in both sources. Cache reads in particular make totals much larger than input plus output.
- Plan history only exists from when sampling started (plus anything imported), because the plan APIs return only the current percentage.
