# Data contract (shared by every part of the combined dashboard)

Every module, script and page in this project follows this file. If code and this file disagree, fix the code or update this file in the same change.

## House rules

- **Australian English.** Dates in the UI are en-AU (dd/mm/yyyy, 24-hour). Numbers are formatted with `en-AU`. Money is shown as `US$`.
- **No em dashes** in any text, comments or docs.
- **Python 3.9+, standard library only.** No pip dependencies. Frontend is vanilla JS plus the vendored Chart.js 4.4.1. No build step.
- **Never log** tokens, cookies, prompts or message content.
- **Source projects are read-only.** Never create, edit, rename, move or delete anything under:
  - `C:\AI-Coding\Personal\Claude Token Usage Graphs`
  - `C:\AI-Coding\Personal\Token Usage Graph`

  Reading their files is fine, including their `data\` files, which the parity check reads.
- All files live under `ROOT = C:\AI-Coding\Personal\Claude and Cursor Combined Token Usage Graph`. Runtime data lives in `ROOT\data\`, which is gitignored and never packed.

## Constants (`collector/util.py`)

| Name | Value | Meaning |
|---|---|---|
| `MINUTES` | 43200 | 30 days of 1-minute buckets |
| `TOKEN_REFRESH_S` | 60 | collector schedule and page poll |
| `PLAN_INTERVAL_S` | 590 | plan lookup throttle per platform (about 10 minutes, as in the Claude source) |
| `DEFAULT_PORT` | 8421 | |
| `DATA_DIR` | `ROOT\data` | overridden by the `CCU_DATA_DIR` environment variable (read at import) |
| `DB_PATH` | `DATA_DIR\usage.db` | |
| `USAGE_JSON` | `DATA_DIR\usage.json` | |
| `CLAUDE_HOOK_LOG` | `DATA_DIR\claude-subagents.jsonl` | |
| `CURSOR_HOOK_LOG` | `DATA_DIR\cursor-subagent-events.jsonl` | |

## Folder layout

```
collector/__init__.py
collector/util.py         constants, atomic_write_json, FileLock, get_logger, iso, parse_ts, claude_dir, cursor_state_db
collector/schema.py       dataclasses below
collector/effort.py       normalise_effort, split_cursor_model
collector/store.py        Store (SQLite)
collector/aggregate.py    build_platform_payload, build_usage_json
collector/plan/__init__.py      maybe_lookup (shared 10-minute throttle)
collector/plan/claude_plan.py   fetch(log) -> (status, pools)
collector/plan/cursor_plan.py   fetch(log) -> (status, pools)
collector/adapters/__init__.py
collector/adapters/claude.py    collect(store, now, log, backfill=False) -> dict
collector/adapters/cursor.py    collect(store, now, log, backfill=False) -> dict
collector/collect.py      entry point: python -m collector.collect [--backfill]
collector/import_history.py   one-off: python -m collector.import_history [--claude-src P] [--cursor-src P] (plan samples from the old apps, read-only, idempotent)
hooks/claude_subagent_hook.py
hooks/cursor_subagent_hook.py
server.py                 python server.py [--port 8421]
web/index.html, web/css/app.css, web/js/*.js, web/vendor/chart.umd.min.js
ccu/cli.py, ccu/scheduler/ (per-OS scheduler back ends)
tests/test_*.py (unittest), tests/fixtures/*, tests/parity_check.py
docs/*.md
```

Run everything from ROOT. Imports are absolute (`from collector.store import Store`). Tests run with `python -m unittest discover -s tests -t .`.

## `collector/util.py`

```python
def atomic_write_json(path, obj) -> None     # write to path + ".tmp<pid>", then os.replace, retrying 10 times (Windows locks)
class FileLock:                              # context manager; O_EXCL lock file; stale after 300 s; raises LockBusy if held
    def __init__(self, path, stale_s=300)
def get_logger(name="collect") -> logging.Logger   # DATA_DIR/<name>.log, rotated to .1 at 1 MB; lines stamped in local time with offset, e.g. 2026-10-21T14:11:25+10:00
def iso(ts: float) -> str                    # "2026-10-21T03:51:03Z" (UTC)
def parse_ts(s) -> float | None              # ISO 8601 (Z or offset) or epoch ms/s -> epoch seconds
def claude_dir() -> Path                     # CLAUDE_CONFIG_DIR or ~/.claude
def cursor_state_db() -> Path                # %APPDATA%\Cursor\User\globalStorage\state.vscdb
```

## `collector/schema.py`

```python
@dataclass
class UsageRecord:
    platform: str            # "claude" | "cursor"
    agent_kind: str          # "primary" | "subagent"
    agent_id: str            # "" for primary; Claude agent id, or Cursor subagent conversation id
    session_id: str          # Claude sessionId, or Cursor conversationId ("" if unknown)
    model: str               # base model, without effort suffix; "Auto Cost" for default/auto/synthetic/unknown
    effort_level: str        # "" | "low" | "medium" | "high" | "extra" | "max" | other lowercase word
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float          # direct cost (Cursor chargedCents/100). Claude: 0.0, allocated at export
    timestamp: float         # epoch seconds
    requests: int = 1
    @property
    def total_tokens(self) -> int   # input + output + cache_read + cache_write (unchanged from both sources)

@dataclass
class SubagentRun:
    platform: str
    agent_id: str            # unique per platform
    agent_type: str          # e.g. "general-purpose", "explore"; "" if unknown
    agent_name: str          # display name, e.g. "explore · a1b2c3d"
    session_id: str          # parent session / conversation
    model: str
    effort_level: str
    t: float                 # end (or latest-seen) time, epoch seconds
    status: str = ""         # Cursor hook status; Claude "completed"
    input_tokens: int = 0    # from the hook (Claude) when the transcript is gone; otherwise recomputed at export
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
```

## `collector/effort.py`

```python
EFFORTS = ("low", "medium", "high", "extra", "max")
def normalise_effort(raw) -> str
    # lowercase and strip; "xhigh", "extra_high", "extra-high" -> "extra"; None or "" -> ""
def split_cursor_model(name) -> tuple[str, str]
    # "grok-4.7-high" -> ("grok-4.7", "high")
    # "claude-opus-5-5-medium" -> ("claude-opus-5-5", "medium")
    # "gpt-5-xhigh" -> ("gpt-5", "extra")
    # "Claude Opus 5.5 (Auto Balanced)" -> ("Claude Opus 5.5", "balanced")
    # "grok-4.7-high (Auto Balanced)" -> ("grok-4.7", "high (auto balanced)")   # both the suffix and the parenthesis are stripped
    # "Claude Opus 5.5 (High)" -> ("Claude Opus 5.5", "high")
    # "-thinking" suffix -> effort "thinking"
    # no suffix -> (name, "")
    # "Auto Cost" -> ("Auto Cost", "")
```

## `collector/store.py` (SQLite, `DB_PATH`, WAL mode)

Tables:

```sql
usage_minute(platform TEXT, minute INT, session_id TEXT, agent_kind TEXT, agent_id TEXT, model TEXT, effort TEXT,
             input INT, output INT, cache_read INT, cache_write INT, cost REAL, requests INT,
             PRIMARY KEY(platform, minute, session_id, agent_kind, agent_id, model, effort))
session_tokens(platform TEXT, session_id TEXT, model TEXT, tokens INT,
               PRIMARY KEY(platform, session_id, model))   -- lifetime totals, never pruned while the session has rows; used for cost allocation
session_cost(platform TEXT, session_id TEXT, model TEXT, cost REAL,
             PRIMARY KEY(platform, session_id, model))     -- Claude cost-state; the last value wins
subagent_runs(platform TEXT, agent_id TEXT, agent_type TEXT, agent_name TEXT, session_id TEXT, model TEXT,
              effort TEXT, t REAL, status TEXT, input INT, output INT, cache_read INT, cache_write INT,
              PRIMARY KEY(platform, agent_id))
plan_samples(platform TEXT, t REAL, pools TEXT /* JSON {pool_id: {"pct": float, "resets_at": str|null}} */,
             PRIMARY KEY(platform, t))
seen(platform TEXT, key TEXT, minute INT, val INT DEFAULT 0, PRIMARY KEY(platform, key))   -- val: output tokens already counted (Claude)
kv(key TEXT PRIMARY KEY, value TEXT /* JSON */)
```

`Store` API:

```python
class Store:
    def __init__(self, path=DB_PATH)
    def close(self); __enter__/__exit__ (commit on success)
    def commit(self)
    def add_usage(self, rec: UsageRecord) -> None        # minute = int(rec.timestamp // 60); sums into usage_minute AND session_tokens
    def seen_add(self, platform, key, minute, val=0) -> bool    # True if new (insert), False if already seen
    def seen_raise(self, platform, key, val) -> int | None      # larger val for a seen key: returns the previous val, else None
    def add_plan_sample_if_absent(self, platform, t, pools) -> bool   # used by import_history
    def set_session_cost(self, platform, session_id, model, cost) -> None
    def upsert_subagent_run(self, run: SubagentRun) -> None   # merges: non-empty fields win, t = max
    def add_plan_sample(self, platform, t, pools: dict) -> None
    def kv_get(self, key, default=None); def kv_set(self, key, value)
    def usage_rows(self, platform, since_minute) -> list[sqlite3.Row]
    def session_tokens(self, platform) -> dict[(session_id, model), int]
    def session_costs(self, platform) -> dict[(session_id, model), float]
    def subagent_runs(self, platform, since_t) -> list[sqlite3.Row]
    def plan_samples(self, platform, since_t) -> list[tuple[float, dict]]
    def prune(self, cutoff_minute) -> None
        # deletes usage_minute and seen rows older than the cutoff, plan_samples and subagent_runs older than 30 days,
        # and session_tokens/session_cost for sessions with no usage rows left
```

## Plan lookup throttle (`collector/plan/__init__.py`)

```python
def maybe_lookup(store, platform, fetch_fn, now, log) -> dict
    # kv "plan:<platform>" = {"last_ok": t, "last_attempt": t, "status": str}
    # If last_ok exists and now - last_ok < PLAN_INTERVAL_S: no HTTP call, return the stored state.
    # Otherwise call fetch_fn(log) -> (status, pools). If pools: add_plan_sample and set last_ok = now.
    #   A failure does not move last_ok, so it retries on the next minute (same as the Claude source).
    # Returns {"status", "last_lookup": iso(last_ok) or None, "next_lookup": iso(last_ok + PLAN_INTERVAL_S) or None}
```

- `fetch(log)` in each plan module returns `(status, pools)`.
  - `status` is `"ok"` or a short user-facing message, for example `"sign-in expired, run claude once"` or `"plan API unreachable (offline?)"`.
  - `pools` is a dict `{pool_id: {"pct": float, "resets_at": iso-or-None}}`, or `None` on failure.
- **Claude pool ids** are the API keys, for example `five_hour`, `seven_day`, `seven_day_opus`.
- **Cursor pool ids** are `cursor_models` and `other_models`.
- **Pool labels** (set in `aggregate.py`):
  - `five_hour`: "5-hour session"
  - `seven_day`: "7-day all models"
  - `seven_day_opus`: "7-day Opus"
  - `seven_day_sonnet`: "7-day Sonnet"
  - `cursor_models`: "Cursor Models"
  - `other_models`: "Other Models"
  - anything else: the id, title-cased.

## Adapters

```python
def collect(store, now, log, backfill=False) -> dict   # {"status": "ok" | "<message>", "meta": {...}}
```

**Claude** (`collector/adapters/claude.py`, ported from `Claude Token Usage Graphs\collect_v1.py`)
- **Reading:**
  - Incremental byte offsets are kept in kv `claude:offsets`.
  - Main files are `projects/*/*.jsonl`; subagent files are `projects/*/*/subagents/agent-*.jsonl`. Files older than 30 days are skipped.
  - De-duplicate with `seen_add("claude", "msgid|reqid", minute, output_tokens)`. A streamed message is written several times and the first copy can hold a partial `output_tokens`; a later copy with a larger value adds only the growth in output tokens (`seen_raise`), never the repeated input or cache tokens, and never another request.
- **What each assistant line becomes:** one UsageRecord.
  - `agent_kind` is `"subagent"` if `isSidechain` is set or the line comes from a subagent file.
  - `agent_id` comes from the file stem `agent-<id>` (subagent files) or `o.get("agentId")`.
  - `effort` comes from `perTurnEffort` or `effort`, normalised.
  - `model` is the label, with "Auto Cost" for default, auto, synthetic or empty.
  - Tokens are kept split into input, output, cache_read and cache_write.
- **Cost:** `cost-state` lines are recorded with `set_session_cost` per model label.
- **Subagent runs:** from `CLAUDE_HOOK_LOG` (format below) plus the subagent files, keyed by agent_id.
  - `agent_name` = `"<agent_type or 'subagent'> · <first 7 chars of agent_id>"`.
- **Meta:** `auto_label_requests`, `files`, `requests`.

**Cursor** (`collector/adapters/cursor.py`, ported from `Token Usage Graph\Fetch-CursorUsage-v1.py`)
- **Sign-in and fetching:**
  - The access token comes from `state.vscdb`, opened read-only.
  - The usage events endpoint is tried first, then the api2 fallback, with the same headers and paging as the source.
  - **Incremental:** kv `cursor:last_ts` holds the newest event time in ms. A normal run fetches from `last_ts - 2 h`; backfill (or no `last_ts`) fetches 30 days.
  - De-duplicate with `seen_add("cursor", f"{timestamp}|{conversationId}|{model}|{in}|{out}|{cache}|{chargedCents}", minute)`.
- **What each event becomes:** one UsageRecord.
  - `model` and `effort` come from `split_cursor_model(resolved model name)`, using the source's model resolution.
  - `cost_usd` = chargedCents / 100.
  - `cache_read` and `cache_write` are kept separate.
  - `agent_kind` = `"subagent"` when the event's conversationId is a subagent conversation recorded in `CURSOR_HOOK_LOG`; otherwise `"primary"`.
- **Subagent runs:** from `CURSOR_HOOK_LOG`, ignoring `subagentType == "hook-self-test"`.
- **Meta:** `event_count`, `headless_requests`, `default_fallback_requests`, `truncated`, `tokens_available`.

## Hook log formats (one JSON object per line)

`CLAUDE_HOOK_LOG` (same fields as `hook_subagent_v1.py`):
```json
{"t":"<iso>","session":"...","agent_type":"...","agent_id":"...","model":"...","effort":"...","input":0,"output":0,"cache_read":0,"cache_write":0}
```

`CURSOR_HOOK_LOG` (same fields as the Cursor hook records):
```json
{"hook":"subagentStart|subagentStop","recordedAt":"<iso>","conversationId":"...","parentModel":"...","subagentId":"...","subagentType":"...","subagentModel":"...","parentConversationId":"...","status":"...","durationMs":0,"messageCount":0,"toolCallCount":0}
```

## Cost allocation (`aggregate.py`)

- **Claude:**
  - For each usage row, `cost = session_cost[(session, model)] * row_tokens / session_tokens[(session, model)]`.
  - If the session has no cost yet, `cost = row_tokens * rate[model]`, where `rate = sum(cost) / sum(tokens)` over costed sessions of that model. These rows are `cost_estimated`, counted in `meta.sessions_estimated` and `meta.estimated_cost_usd`.
  - If there is no rate either, the cost is 0 and the row counts in `meta.sessions_without_cost`.
- **Cursor:** the row's own `cost`.

## API: `GET /api/usage.json` (file `USAGE_JSON`, rebuilt every minute by `collect.py`)

```json
{
  "generated_at": "2026-10-21T03:51:03Z",
  "token_refresh_s": 60,
  "plan_refresh_s": 590,
  "platforms": {
    "claude": {
      "status": "ok",
      "start": "2026-09-21T03:52:00Z",
      "minutes": 43200,
      "series": [
        {"key": "claude-opus-5-5-medium", "model": "claude-opus-5-5", "effort": "medium",
         "t": [15675, 30380],
         "input": [120, 80], "output": [900, 400], "cache": [337285, 110740], "cost": [0.764384, 0.057803], "requests": [3, 1],
         "sub_input": [0, 10], "sub_output": [0, 50], "sub_cache": [0, 2000], "sub_cost": [0.0, 0.0012],
         "cache_read": [300000, 100000], "sub_cache_read": [0, 1800]}
      ],
      "subagent_runs": [
        {"agent_id": "a1b2c3d4", "agent_name": "explore · a1b2c3d", "agent_type": "explore", "session_id": "...",
         "model": "claude-sonnet-5", "effort": "medium", "t": "2026-10-21T03:40:00Z", "status": "completed",
         "input": 1200, "output": 3400, "cache": 250000, "cost": 0.41, "cost_estimated": false}
      ],
      "plan": {
        "status": "ok", "last_lookup": "2026-10-21T03:43:03Z", "next_lookup": "2026-10-21T03:52:53Z",
        "pools": [{"id": "five_hour", "label": "5-hour session", "now": 9.0, "remaining": 91.0, "resets": "2026-10-21T06:29:59Z"}],
        "samples": [{"t": "2026-10-21T03:43:03Z", "five_hour": 9.0, "seven_day": 10.0}]
      },
      "meta": {"auto_label_requests": 8, "sessions_without_cost": 0, "sessions_estimated": 3, "estimated_cost_usd": 30.52, "requests": 2975, "files": 78}
    },
    "cursor": {
      "...": "same shape",
      "meta": {"event_count": 207, "headless_requests": 21, "default_fallback_requests": 45, "truncated": false, "tokens_available": true, "subagent_tokens_attributed": false}
    }
  }
}
```

### Series
- `t` is minute indexes from `start` (sparse, ascending), where `start` = `(now_minute - MINUTES + 1)` as ISO. Only minutes with data appear.
- A series is one (model, effort) pair. `key` is `model-effort`, or just `model` when the effort is empty.
- `input`, `output`, `cache` and `cost` are **all** usage, primary plus subagents. The `sub_*` arrays are the subagent part of those totals.
- `cache` = cache_read + cache_write. Tokens = input + output + cache.
- `cache_read` and `sub_cache_read` are extra per-minute arrays (same `t` indexing and length as `cache`/`sub_cache`). `cache_read` is a subset of `cache` (cache READ tokens only); `sub_cache_read` is the subagent part. Older frontends ignore them. Cache hit rate = cache_read / (input + cache_read + cache_write), computed client-side.
- Series are sorted by total tokens, highest first.

### Subagent runs
- `input`, `output`, `cache` and `cost` are recomputed from `usage_minute` rows with that agent_id. They fall back to the hook token counts when there are no rows.
- Cursor runs with no attributed rows have `input`, `output`, `cache` and `cost` set to `null`. The UI shows these as "n/a".

### Plan samples
- Samples older than 2 days are thinned to one per 5 minutes.
- `remaining` = `max(0, 100 - now)`.

### Clients not installed
- Each run, `collect.py` checks whether each client is on this PC:
  - **Claude:** `claude_dir()/projects` or `.credentials.json` exists.
  - **Cursor:** `cursor_state_db()` exists.
- A missing client is not collected or plan-checked, and its payload is just `{"installed": false, "status": "not installed"}`. Installed platforms carry `"installed": true`.
- The page hides that platform's column and shows the other one full width. If neither is installed it shows a notice.
- Once a client is installed, the next run collects it: Claude from the start of its transcripts, Cursor with a 30-day first fetch. Its column appears on the next page refresh, with no reload needed.

### Platform errors
- If a platform fails entirely, its `status` holds the message. Its other fields keep the last data in the store.

## Server (`server.py`)

- Binds to `127.0.0.1` only. A client that is not loopback gets a 403. Only GET and HEAD are allowed; anything else gets a 405.
- Every response is sent with `Cache-Control: no-store`.

| Route | Response |
|---|---|
| `/` or `/index.html` | `web/index.html` |
| `/web/<path>` | a static file from `web/`, after a path-traversal check |
| `/api/usage.json` | `USAGE_JSON`. If the file is missing: 404 with `{"error":"usage.json not generated yet"}` |
| `/api/health` | `{"ok": true, "generated_at": <from USAGE_JSON or null>}` |

## Frontend window behaviour (from both sources)

- **Windows:** 30m=30, 1h=60, 6h=360, 12h=720 (the default), 24h=1440, 7d=10080 and 30d=43200 minutes.
- **Labels:** "30 min", "1 hour", "6 hours", "12 hours", "24 hours", "7 days", "30 days".
- **Window anchoring:**
  - `endIdx = floor((Date.now() - Date.parse(start)) / 60000)`
  - `loIdx = endIdx - W + 1`
- **Charts:** cumulative lines per series inside the window, with Chart.js LTTB decimation at 500 samples.
- **Refresh:** the page fetches `/api/usage.json` every 60 s with `cache: 'no-store'` and shows a "Next refresh in" countdown.
- **localStorage:** the window and metric are remembered in `ccu.window` and `ccu.metric`, accessed inside try/catch.
