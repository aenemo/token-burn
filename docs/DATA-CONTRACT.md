# Data contract (shared by every part of the combined dashboard)

Every module, script and page in this project follows this file. If code and this file disagree, fix the code or update this file in the same change.

## House rules

- **Australian English.** Dates in the UI are en-AU (dd/mm/yyyy, 24-hour). Numbers are formatted with `en-AU`. Money is shown as `US$`.
- **No em dashes** in any text, comments or docs.
- **Python 3.9+, standard library only.** No pip dependencies. Frontend is vanilla JS plus the vendored Chart.js 4.4.1. No build step.
- **Never log** tokens, cookies, prompts or message content.
- **Source projects are read-only.** Never create, edit, rename, move or delete anything in the two original dashboards this project was built from (the Claude and the Cursor token usage graphs).

  Reading their files is fine, including their `data\` files, which the parity check reads.
- `ROOT` below means the root of this repository. Installed runtime data lives in the per-user data folder (see `docs/CONFIGURATION.md`); a source checkout can use `ROOT\data\`, which is gitignored and never packed.

## Constants (`collector/util.py`)

| Name | Value | Meaning |
|---|---|---|
| `MINUTES` | 43200 | 30 days of 1-minute buckets |
| `TOKEN_REFRESH_S` | 60 | collector schedule and page poll |
| `PLAN_INTERVAL_S` | 590 | plan lookup throttle per platform (about 10 minutes, as in the Claude source) |
| `DEFAULT_PORT` | 8421 | |
| `DATA_DIR` | per-OS data folder | `TOKEN_BURN_DATA_DIR` environment variable, then the `location.json` pointer, then the per-OS default (read at import) |
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
token_burn/cli.py, token_burn/scheduler/ (per-OS scheduler back ends)
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
def opencode_db() -> Path                    # TOKEN_BURN_OPENCODE_DB, else $XDG_DATA_HOME or ~/.local/share, then opencode/opencode.db (all OSes)
```

## `collector/schema.py`

```python
@dataclass
class UsageRecord:
    platform: str            # "claude" | "cursor" | "opencode" | "openai"
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
session_meta(platform TEXT, session_id TEXT, title TEXT, source TEXT, updated REAL,
             PRIMARY KEY(platform, session_id))      -- session titles; source is ai, chat, app or custom
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
    def upsert_session_title(self, platform, session_id, title, source, t=0.0) -> bool
        # title cleaned by clean_title() (control characters removed, whitespace collapsed, 120 characters);
        # a custom title is never replaced by ai, chat or app; an equal source replaces; True when it changed
    def session_ids(self, platform) -> list[str]       # distinct non-empty session ids in usage_minute
    def session_titles(self, platform) -> dict[session_id, title]
    def prune(self, cutoff_minute) -> None
        # deletes usage_minute and seen rows older than the cutoff, plan_samples and subagent_runs older than 30 days,
        # and session_tokens/session_cost/session_meta for sessions with no usage rows left
```

### Session titles

Titles come only from these sources, never from prompt text (Claude `last-prompt` entries are never read):

| Platform | Source | Rank |
|---|---|---|
| Claude | `ai-title` entries (`aiTitle`), the latest wins | `ai` |
| Claude | `custom-title` entries (`customTitle`, set with `/rename`), the latest wins | `custom` |
| opencode | `session_v2.title` of top-level sessions | `app` |
| OpenAI | The Codex thread `name` field only (never `title`, `preview` or the first prompt), plus the opencode title for OpenAI requests made through opencode | `app` |
| Cursor | `name` (else `subtitle`) of `composerData:<conversation id>` in this computer's `state.vscdb`, read-only; each untitled conversation is looked up at most once a day (kv `cursor:title_checked`) | `chat` |
| Cursor | `title` in `conversation-search.db` (`conversations` table, next to `state.vscdb`), read-only; also names cloud agents (`bc-` ids) | `chat` |
| Cursor | opencode session title for agent runs started in an OpenChamber chat folder. The project slug ends `-session-<uuid>`, and that uuid is found in `session_v2.directory` in the opencode database (read-only, `TOKEN_BURN_OPENCODE_DB` overrides). The top-level session's title is used (a child session uses its parent's); default "New session - <date>" titles are ignored. Local only like a project label, so `title_local: true`, never exported, rechecked daily and replaced by any real name | `project` |
| Cursor | Project label "Agent <6 hex> · <project>" for runs whose transcript is in `~/.cursor/projects/<slug>/agent-transcripts/` (`CURSOR_PROJECTS_DIR` overrides). The slug is the workspace path with separators as dashes; it is resolved against the disk to get the real folder name, skipping ids, dates and generic folders. Local only: `title_local: true` in `usage.json`, never exported, rechecked daily and replaced by any real name | `project` |

Cursor placeholder names ("New Agent", "New Chat", "Untitled") are never used. Ranks: `project` 0, `ai`/`chat`/`app` 1, `custom` 2; a higher rank is never replaced by a lower one.

Claude transcripts already read by an older version are scanned once for title lines only (kv `claude:titles_v1`). Subagent transcripts are skipped.

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
- **Cursor and opencode:** the row's own `cost`. For opencode `meta.cost_reported` is false if any row in the window has tokens but zero cost (an empty window is true); when false the page shows a note that some opencode models (free or gateway) report no cost, so their cost shows as US$0.00.

**OpenAI** (`collector/adapters/openai.py`, with the OpenAI routing in the opencode adapter)
- Codex (app and CLI): reads `<codex home>/state_<n>.sqlite` (newest `n`) read-only for the thread list, and each thread's rollout JSONL for `token_count` events. Only timestamps, token counts, model and effort are parsed; message text and the sign-in file are never read. Per-thread byte offset and last total are kept in kv `openai:offsets`. Input is `input_tokens` minus cached minus cache write, cache read is `cached_input_tokens`, output is `output_tokens` (reasoning is already included). Child threads (spawn edges) are subagent runs.
- opencode requests whose provider is `openai` are filed under this platform instead of opencode (no double counting). On upgrade, earlier `openai/` rows are removed from opencode once (kv `opencode:openai_split`) and re-read.
- Cost: the stored cost is 0 for Codex and for opencode ChatGPT-plan requests. At aggregation time (`collector/aggregate.py`, prices in `collector/openai_prices.py`) a row with no cost is valued at OpenAI's published API list prices (standard short-context rates, cached input at the cached rate, cache writes at 1.25x input on GPT-6 models), and a row that reported its own cost keeps it. These are API costs only, not what a ChatGPT or Codex subscription costs. `meta.cost_estimated` is true when any row was priced this way, `meta.cost_api_list` is always true, `meta.models_unpriced` lists models with tokens but no listed price (their cost stays 0), and `meta.cost_reported` is false only when such models exist. Long-context, Batch, Flex, Fast and regional pricing are not modelled. Peers on 0.5.0 send 0 cost for these rows. The ChatGPT web and desktop apps keep no token counts locally and cannot be tracked.
- Plan (`collector/plan/openai_plan.py`): no network calls. Reads the newest `rate_limits` block from the `token_count` events in the 8 most recently written rollout files (tail only). `primary` and `secondary` windows with a `used_percent` become pools (`five_hour` for a 300 minute window, `seven_day` for 10080, else `primary` or `secondary`), skipped once their reset time has passed. The plan type and credits (`unlimited`, `has_credits`, `balance`) are stored beside the pools as an `_info` entry (no `pct`, so it is never drawn or synced) and appear as `plan.info` in the payload. Business or unlimited plans report no windows and no balance, so only `plan.info` is set; the page then shows a Credits tile and a plan note, and a Credit balance tile when a balance is reported. Peers on older versions still count OpenAI inside opencode.

**opencode** (`collector/adapters/opencode.py`)
- Reads `opencode.db` strictly read-only (see `docs/CONFIGURATION.md` for the path). Only the v2 layout (`session_v2` and `session_message`) is supported; otherwise the status is "unsupported opencode data format". Credential tables and message text are never read.
- Each `assistant` or `compaction` message with tokens is one usage row. `model` is `provider/model`, `effort` is the model variant, `output` includes reasoning tokens, and `cost` is the recorded cost (often 0). A message still streaming is skipped until it completes.
- A session with a parent is a subagent (`agent_id` is the session id); subagent runs are built from these rows.
- Incremental: kv `opencode:last_ts`, with a 2 hour overlap and a 30 day first read. De-duplicated by message id.
- Plan: none (`collector/plan/opencode_plan.py`), so its plan block carries no pools.
- Cost: the row's own cost, as for Cursor.

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
      "sessions": [
        {"id": "3c9d...", "title": "Fix the login page",
         "series": [{"key": "claude-opus-5-5-medium", "model": "claude-opus-5-5", "effort": "medium",
                     "t": [15675], "input": [120], "output": [900], "cache": [337285], "cost": [0.764384]}]}
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
    },
    "opencode": {
      "...": "same shape; plan has no pools",
      "meta": {"requests": 480, "subagent_tokens_attributed": true, "cost_reported": false}
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

### Sessions
- One entry per session with usage in the 30-day history, sorted by total tokens, highest first. `title` is `null` when no title is known; the page then shows "Session " plus the first 8 characters of the id (Cursor: "Conversation " plus 6, or "Cursor agent " plus 6 for `agent-` ids).
- `series` is grouped by model key like the top-level series, with the same `t` indexing and allocated cost, and includes subagent usage in that session (opencode child sessions count under their parent). Summed over all sessions it equals the top-level series.

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
  - **opencode:** `opencode_db()` exists.
- A missing client is not collected or plan-checked, and its payload is just `{"installed": false, "status": "not installed"}`. Installed platforms carry `"installed": true`.
- The page disables that provider's header toggle and hides its column; the visible columns share the width. If none is installed it shows a notice.
- Once a client is installed, the next run collects it: Claude from the start of its transcripts, Cursor with a 30-day first fetch, opencode with a 30-day first read. Its column appears on the next page refresh, with no reload needed.

### Platform errors
- If a platform fails entirely, its `status` holds the message. Its other fields keep the last data in the store.

## Multi-computer sync (version 0.2.0)

Optional. With sync off (no `config.json`, or `"sync": {"enabled": false}`) the `sync` and `machines` keys are absent and `usage.json` is exactly as described above. The frontend treats an absent `sync` key as sync off. Older frontends ignore the new keys.

### New top-level keys of `usage.json`

```json
"machines": [{"id": "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7", "name": "Office PC", "os": "Windows 11", "hostname": "OFFICE-PC",
  "app_version": "0.2.0", "is_self": true, "last_seen": "2026-11-05T03:00:00Z", "state": "active", "stale": false,
  "status": "ok", "superseded_by": null, "superseded_by_name": null,
  "platforms": ["claude", "cursor"], "tokens_30d": 123456789}],
"sync": {"enabled": true, "status": "ok", "folder_found": true, "last_export": "2026-11-05T03:00:00Z",
  "ignored_files": 1, "messages": ["MacBook: incomplete or damaged (still syncing?); showing the last good copy"]}
```

- `machines` appears only when sync is enabled. Sorted self first, then by name. `state` is `active` or `retired`; removed computers are not listed. `stale` is true when `state` is active and `now - last_seen > 3600` (for this computer, since its last export, or false when an export is pending). `tokens_30d` counts Claude and opencode only (Cursor is account-wide). `name` is the control file's `display_name` if present, else the file's. `superseded_by` and `superseded_by_name` are null, or the id and name of a newer entry for the same physical computer (same `host_fp`, for example after the id was lost); a superseded row is never `stale` and its data is folded into the newer entry.
- `sync.status` is `"ok"` or a message such as `"shared folder not found: <folder>"`, `"sync failed (<ExcName>)"`, or a conflict or removal message. When the folder is missing the local data alone is shown and the export is skipped. `sync.messages` carries per-computer notes (damaged file, newer format, clock ahead by N min, too many computers, not downloaded yet).
- Sync off: the `sync` key and the `machines` key are both absent. The frontend treats absent as off.

### `platforms.claude` and `platforms.opencode`: `machines`

Present whenever 2 or more computers are in the `machines` list, with an entry for every computer that has data for the platform (possibly only one, possibly none: `{}`). Because the key is present, a computer missing from it has no usage for that platform; the frontend only falls back to each computer's installed `platforms` when the key is absent altogether (older collectors). A superseded computer has no entry of its own: its data is folded into the newer entry for the same physical computer.

```json
"machines": {"<machine id>": {"series": [ ... same shape as platforms.<p>.series, t relative to start ... ],
                              "sessions": [ ... same shape as platforms.<p>.sessions ... ],
                              "subagent_runs": [ ... ]}}
```

The platform's own `series`, `sessions` and `subagent_runs` are the merged totals. Merged sessions from other computers carry their hashed id; this computer's own sessions keep their real id. `meta.machines` holds the number of computers. Cursor has no `machines` breakdown because its data is account-wide. The frontend (`web/js/machines.js`) rebuilds the Claude and opencode series from `machines` when only some computers are selected.

### Merge rules (`collector/sync/merge.py`)

Minutes are merged as absolute epoch minutes (a peer's `start` may differ); at the end `t = m - start_minute` and only `0 <= t < MINUTES` is kept. A cell is `[input, output, cache, cost, requests, sub_input, sub_output, sub_cache, sub_cost, cache_read, sub_cache_read]`.

| Platform | Rule |
|---|---|
| Claude, opencode | Cells are summed across this computer and every included peer (retired still counts, removed does not), except that machine ids sharing a `host_fp` (one physical computer that lost its id, was reset or reinstalled, plus this computer's own older ids and `adopted-<id>.json.gz` history) read the same logs, so they are combined per cell by MAX first and then summed with other computers. The newest written member represents the group; older ones are `superseded_by` it. Peers without `host_fp` (older versions) are summed as before. Subagent runs are concatenated, de-duplicated on `agent_id`, sorted newest first and limited to the window. Local `meta` is kept; if this computer does not have the client installed, `subagent_tokens_attributed` is true when any peer run has tokens. opencode `cost_reported` is false if any computer has tokens with cost 0. `status` is the local status, or `"ok (from other computers)"` when not installed locally |
| Cursor | Per (series key, minute) the cell with the largest token total (input + output + cache) wins; ties go to larger cost, then larger requests. The winning cell supplies input, output, cache, cache_read, cost and requests. Each `sub_x` is `min(winning total of x, max of sub_x over all cells)`. Runs are the union by `agent_id`, preferring a record with non-null tokens, else the newest. The same conversation seen on two computers can appear twice: Cursor agent ids are hashed per computer (`sha256(machine_id:id)`, for privacy), so run de-duplication across computers cannot match and the union can list a run once per computer. Cell totals are unaffected (largest wins) |
| Sessions | Keyed by the hashed session id (below). Within one physical computer (same `host_fp`) the same id is combined per cell by MAX; different computers are unioned. Cursor ids are salted account-wide, so one conversation seen on several computers is one session, combined by MAX. The first title wins and a later one fills a missing title |
| Plans (Claude, Cursor) | Samples are the union of local samples (last 30 days) and peer samples, de-duplicated on `round(t)`; current pools come from the newest sample from any computer, reset times are the union, `last_lookup` is the newest across computers, `next_lookup` and `status` are local. Computers on different accounts would interleave (not supported) |

### Shared folder layout

```
<synced folder>/token-burn/
  README.txt
  machines/<machine_id>.json.gz     written only by that computer
  control/<machine_id>.json         written by any computer
```

`machine_id` matches `^[0-9a-f]{32}$` and is the only thing ever used to build a path; display names never touch paths. In `machines/`, any name not exactly `<id>.json.gz` (conflict copies, `.tmp*`, iCloud stubs) is ignored and counted in `ignored_files`; an iCloud stub `.<id>.json.gz.icloud` reports "not downloaded yet".

### Machine file `machines/<id>.json.gz` (gzip level 6, `mtime=0`, JSON)

```json
{"format": "token-burn-machine", "schema": 1,
 "machine_id": "3f2a9c0e5b7d4e1fa0b1c2d3e4f5a6b7", "display_name": "Office PC", "hostname": "OFFICE-PC",
 "host_fp": "0f1e2d3c4b5a6978", "os": "Windows 11", "app_version": "0.2.0", "written_at": "2026-11-05T03:00:00Z",
 "write_token": "9f1c2a7b3d4e5f60", "minutes": 43200,
 "platforms": {
  "claude": {"installed": true, "status": "ok",
   "series": [{"key": "claude-opus-5-5-medium", "model": "claude-opus-5-5", "effort": "medium",
     "m": [29631234, 29631240], "input": [120, 80], "output": [900, 400], "cache": [337285, 110740],
     "cache_read": [300000, 100000], "cost": [0.764384, 0.057803], "requests": [3, 1],
     "sub_input": [0, 10], "sub_output": [0, 50], "sub_cache": [0, 2000], "sub_cache_read": [0, 1800], "sub_cost": [0.0, 0.0012]}],
   "sessions": [{"id": "5e0b7a9c21d4f368", "title": "Fix the login page",
     "series": [{"key": "claude-opus-5-5-medium", "model": "claude-opus-5-5", "effort": "medium",
       "m": [29631234], "input": [120], "output": [900], "cache": [337285], "cost": [0.764384]}]}],
   "subagent_runs": [{"agent_id": "b81f0c2e9a7d4c11", "agent_name": "explore \· b81f0c2", "agent_type": "explore",
     "model": "claude-sonnet-5", "effort": "medium", "t": "2026-11-05T02:40:00Z", "status": "completed",
     "input": 1200, "output": 3400, "cache": 250000, "cost": 0.41, "cost_estimated": false}],
   "plan": {"last_lookup": "2026-11-05T02:55:00Z",
            "samples": [[1793500000.0, {"five_hour": {"pct": 9.0, "resets_at": "2026-11-05T06:29:59Z"}}]]}},
  "cursor": {"installed": false},
  "opencode": {"installed": true, "status": "ok", "series": [], "subagent_runs": [], "plan": {"last_lookup": null, "samples": []}}}}
```

- `m` is absolute epoch minutes. Series are built from this computer's own payloads before merging, so merged data is never re-exported.
- Never written: raw session ids, `meta`, file counts, paths, prompts, credentials. Session ids are `sha256("session:" + salt + ":" + id).hexdigest()[:16]`, where the salt is `host_fp` (else the machine id) for Claude and opencode and `cursor` for Cursor. Titles are only those listed under Session titles except `project` labels (exported as `null`), capped at 120 characters with control characters removed. `agent_id` is `sha256(machine_id + ":" + original_id).hexdigest()[:16]` and `agent_name` is rebuilt as `<type or "subagent"> \· <hashed id[:7]>`. `status` is trimmed to 120 characters with control characters removed.
- Plan samples are raw `(t, pools)`, thinned to one per 5 minutes after 2 days; per pool only a finite `pct` and a `resets_at` string of at most 40 characters are kept.
- `host_fp` (optional, 16 lowercase hex; ignored when invalid) identifies the physical computer: the first 16 hex of SHA-256 of `token-burn-host-fp-v1:` plus the OS machine id (Windows `MachineGuid` read-only from the registry, macOS `IOPlatformUUID` from `ioreg`, Linux `/etc/machine-id` or `/var/lib/dbus/machine-id`), or of the host name and OS when none is readable. The raw id is never exported. Older files have none.
- A computer adopted after a reinstall merges `data_dir/adopted-<id>.json.gz` into its export by per-cell maximum; the file is deleted once all its minutes are older than 30 days.
- Writing: a temp file `machines/.tmp-<id>-<pid>.json.gz` then `os.replace` with retries; own temp files older than 1 hour are deleted; POSIX file mode 0600 and directories 0700 (best effort). A write happens at most every 300 s, only when the SHA-256 of the canonical body (sorted keys, without `written_at` and `write_token`) changed, or when the heartbeat is due (`HEARTBEAT_S` = 1800 s since the last write).

### Control file `control/<id>.json` (plain JSON, 4 KB cap)

```json
{"format": "token-burn-control", "schema": 1, "machine_id": "3f2a...", "state": "active",
 "display_name": "Old office PC", "by": "<writer machine id>", "at": "2026-11-05T03:00:00Z"}
```

`state` is `active`, `retired` (data still counts, no stale warning, listed and toggleable) or `removed` (tombstone: peers ignore the computer; the owner stops exporting and reports it was removed). Rejoining deletes the tombstone and creates a new id.

### Validation caps for peer files (`collector/sync/validate.py`)

| Limit | Value |
|---|---|
| Compressed file size | 16 MiB |
| Decompressed size | 64 MiB (read in 1 MiB chunks, aborted above) |
| Machine files read per pass | 64 (newest first; beyond that "too many computers") |
| Series per platform | 300; each array at most 50,000 entries, all equal length |
| Subagent runs per platform | 5,000 |
| Sessions per platform | 3,000; at most 40 series each; id must be 16 lowercase hex (bad or repeated ids are dropped); title at most 120 characters or `null` |
| Plan samples | 20,000; at most 20 pools each; `pct` finite 0 to 1000 |
| Values | tokens integer 0 to 1e12, cost finite 0 to 1e6, requests 0 to 1e7; each `sub_*` clamped to its total |
| Strings | name 40, hostname 64, os 40, version 20, model, key and effort 120, status 120; control characters removed |
| Minutes | strictly ascending integers; those outside `[now_min - MINUTES + 1, now_min + 5]` are dropped, not rejected |
| Clock skew | `written_at` more than 600 s ahead gives "clock is ahead by N min"; last seen is clamped to now |

Files must be regular (symlinks skipped), start with `format` `token-burn-machine` and `schema` 1 (a larger schema shows "made by a newer Token Burn; update this computer"), and carry a `machine_id` equal to the file name. Unknown platforms and fields are ignored. A failing file falls back to the last good copy in `data_dir/peer-cache/<id>.json.gz`, with a message. Nothing from the shared folder is ever evaluated, unpickled, imported or executed.

### `config.json` (data folder)

See [CONFIGURATION](CONFIGURATION.md). Keys: `version` (schema version, stays 1), `app_version` (the Token Burn version that last ran the installer), `install_dir`, `data_dir`, and `sync` (`enabled`, `folder`, `machine_id`, `machine_name`, `joined_at`, `adopted`). Store key-value entries: `sync:last_hash`, `sync:last_write`, `sync:last_token`, `sync:conflict`.

### Conflicting ids

Before writing, the existing own file's header is read. If its `write_token` differs from `sync:last_token` and its `written_at` is later than `sync:last_write`, another computer is using this id: exports stop (merging continues) until `token-burn sync reset-id`. The first write after install finding an existing file proceeds when the host name matches (a reinstall) and is a conflict when it differs.

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
- **localStorage:** the window and metric are remembered in `token-burn.window` and `token-burn.metric`, the visible providers in `token-burn.providers`, the computers switched off in `token-burn.machines` (an object `{machineId: false}`), and each column's monthly plan fee in US$ in `token-burn.fee.<platform>`, accessed inside try/catch.
- **Derived stats (no new keys):** the Input : output tile, the value for money figures and the limit forecast are all computed in `stats.js` from keys already listed here (`input`, `output`, `cache`, `cost` and `plan.samples`, `pools[].now`, `pools[].resets`).
