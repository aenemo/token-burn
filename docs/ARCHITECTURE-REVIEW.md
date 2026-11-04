# Architecture review (Phase 1)

Written 21/10/2026, before the combined dashboard was built. It records how the two existing dashboards work, where they match and differ, and what the combined design reuses. The two source projects were read only; nothing in them was changed.

Sources reviewed:

- Claude: `C:\AI-Coding\Personal\Claude Token Usage Graphs` (written below as `claude/...`)
- Cursor: `C:\AI-Coding\Personal\Token Usage Graph` (written below as `cursor/...`)

Line numbers refer to the files as reviewed.

## 1. Current Claude architecture

| Part | Detail | Reference |
|---|---|---|
| Stack | Python standard library, `ThreadingHTTPServer` on port 8417, one HTML page with vanilla JS and vendored Chart.js 4.4.1 | `claude/server_v1.py`, `claude/web/dashboard_v1.html` |
| Collector | `collect_v1.py` runs every minute from a hidden scheduled task (`\ClaudeTokenUsage\Refresh`). A run holds a lock file (stale after 300 s), reads transcripts, fetches the plan, rewrites `usage.json` and logs to a rotated `collect.log` | `claude/collect_v1.py` (`main` 588, `acquire_lock` 86, `LOCK_STALE_S` 38) |
| Token source | `~/.claude/projects/*/*.jsonl` and `projects/*/*/subagents/agent-*.jsonl`, read incrementally by byte offset and only up to the last complete newline. Offsets live in `state.json` | `claude/collect_v1.py` (`discover` 178, `process_file` 190) |
| De-duplication | Message id plus request id, falling back to the line `uuid` | `process_file` |
| Token total | input + output + cache read + cache write. The split is discarded when a line is summed | `process_file` |
| Model label | `default`, `auto`, `<synthetic>` or empty become "Auto Cost" | `claude/collect_v1.py` (`AUTO_LABELS` 35, `label` 151) |
| Effort | `perTurnEffort` or `effort` on the line; `xhigh`, `extra_high` and `extra-high` map to `extra`. Series are keyed `model-effort` | `claude/collect_v1.py` (`effort_of` 143, `EFFORT_NAMES` 37) |
| Cost | Claude writes `cost-state` lines with `modelUsage[model].costUSD` per session. The collector keeps the latest value per session and model and spreads it over that session's minutes by token share. A running session with no `cost-state` yet is estimated at the per-model rate from finished sessions and flagged in `meta` | `claude/collect_v1.py` (`build_usage` 525) |
| Subagents | Lines with `isSidechain`, or from `subagents/agent-*.jsonl`, count toward the model totals. The `SubagentStop` hook appends `agent_type`, `agent_id`, model, effort and in/out/cache totals to `data/subagents.jsonl`; `subagent_block` summarises by type | `claude/hook_subagent_v1.py`, `claude/collect_v1.py` (`subagent_block` 469) |
| Plan | `GET https://api.anthropic.com/api/oauth/usage` with the OAuth access token from `~/.claude/.credentials.json`. The token is refreshed (`console.anthropic.com/v1/oauth/token`) when expired or on a 401 and written back to Claude's own file. Throttled to one lookup per `PLAN_INTERVAL_S = 590` seconds; a failure retries on the next minute. Samples go to `plan_samples.jsonl` | `claude/collect_v1.py` (`fetch_plan` 342, `do_plan` 414, `plan_block` 440) |
| Storage | `state.json` (per-session, per-minute state), `usage.json` (sparse per-minute arrays per series), `plan_samples.jsonl`, `subagents.jsonl`, logs | `claude/data/` |
| Output shape | `usage.json` with `start`, `minutes` (43200), `models`, `series{name: {t[], tokens[], cost[]}}`, `subagents`, `plan`, `meta` | `claude/collect_v1.py` (`build_usage`) |
| Server | Loopback only (403 otherwise); serves the page and `/usage.json` with no-store caching | `claude/server_v1.py` |
| Frontend | Windows 30m to 30d computed client-side from the sparse arrays. Chart.js with LTTB decimation at 500 samples. Tokens/Cost toggle, click-to-highlight, a "Models in window" list, a "Subagents by type" summary, a plan chart and table (pool, current, start, end, change, resets). `?embed=1` compact mode for the desktop-app pane | `claude/web/dashboard_v1.html` (decimation 358, embed 174) |
| Extras | A Claude desktop-app pane mod that reads the same JSON | `claude/mod/token-usage-pane` |

## 2. Current Cursor architecture

| Part | Detail | Reference |
|---|---|---|
| Stack | Python standard library static server on port 8765; one HTML page in vanilla ES5 with hand-drawn SVG charts (no chart library) | `cursor/Serve-TokenUsageWeb-v1.py`, `cursor/web/index.html` |
| Collector | `Get-CursorTokenUsage-v1.ps1` runs `Fetch-CursorUsage-v1.py` every minute from a hidden scheduled task ("Cursor Token Usage Graph") through a VBS launcher. The fetcher writes a snapshot and the wrapper copies it to the canvas file | `cursor/Get-CursorTokenUsage-v1.ps1`, `cursor/Fetch-CursorUsage-v1.py` (`main` 462) |
| Sign-in | The access token is read from Cursor's `state.vscdb` (`cursorAuth/accessToken`) | `Fetch-CursorUsage-v1.py` (`read_token` 22) |
| Token source | `POST https://cursor.com/api/dashboard/get-filtered-usage-events` (cookie `WorkosCursorSessionToken`), falling back to `https://api2.cursor.sh/aiserver.v1.DashboardService/GetFilteredUsageEvents` (bearer header). Pages of 100, at most 100 pages, with a `truncated` flag when the cap is reached | `Fetch-CursorUsage-v1.py` (462 to 580) |
| Fetch window | Every minute it re-fetches from local midnight (days minus one) ago, up to 30 days: up to 100 pages per run | `Fetch-CursorUsage-v1.py` (484) |
| Token total | `tokenUsage` input + output + cache read + cache write; the split is discarded | `Fetch-CursorUsage-v1.py` (`token_parts` 118) |
| Cost | `chargedCents / 100` per event (fallback `tokenUsage.totalCents`) | `token_parts` |
| Model name | Chosen from many candidate keys (`resolvedModel`, `underlyingModel`, `modelIntent`, any key containing "model"), skipping generic values (`default`, `auto`, `unknown` and so on); otherwise a fallback bucket | `Fetch-CursorUsage-v1.py` (`candidate_model_names` 154, `event_model_name` 199, `GENERIC_MODEL_NAMES` 138) |
| Effort | Only a suffix or parenthesis in the model name (`grok-4.7-high`, `Claude Opus 5.5 (Auto Balanced)`). The source keeps it as part of the model name | model name |
| Subagents | `Record-SubagentUsage-v1.ps1` (Cursor `subagentStart` and `subagentStop` hook) logs type, model and status to `subagent-events.jsonl`. It carries no tokens, so the page can only show runs, and the log has been empty so far | `cursor/Record-SubagentUsage-v1.ps1`, `Fetch-CursorUsage-v1.py` (`load_subagents` 408) |
| Plan | `fetch_plan_percents` tries `GET cursor.com/api/usage-summary`, then `POST api2.cursor.sh/.../GetCurrentPeriodUsage`, then `POST cursor.com/api/dashboard/get-current-period-usage`. A key search finds the Cursor Models and Other Models percentages, with a used-over-limit fallback. It runs **every minute with no throttle**. History is `plan-percent-history.jsonl` | `Fetch-CursorUsage-v1.py` (`fetch_plan_percents` 290, `extract_plan_percents` 245, `flatten_fields` 206, `used_limit_percent` 237, `load_plan_history` 326, `save_plan_sample` 366) |
| Storage | `data/usage-snapshot.json` (about 5.8 MB, dense arrays with one value per minute per model, about 43,000 points per series) plus `plan-percent-history.jsonl` | `cursor/data/` |
| Server | Static server for `index.html` and the snapshot | `cursor/Serve-TokenUsageWeb-v1.py` |
| Frontend | Same 7 windows; `windowSlice` slices the dense arrays with no decimation. SVG cumulative-by-model chart with hover tooltip and click-to-highlight, a totals table, a plan chart and table, Tokens/Cost toggle | `cursor/web/index.html` (`windowSlice` 209, hover 364, legend click 537, refresh 476) |
| Extras | A Cursor canvas template | `cursor/token-usage.canvas.template.tsx` |

## 3. Similarities

| Area | Shared behaviour |
|---|---|
| Time model | Same 7 windows (30m, 1h, 6h, 12h default, 24h, 7d, 30d), 1-minute buckets, 30 days of history |
| Charts | Cumulative-by-model chart, Tokens/Cost toggle, click a model to highlight, plan percentage over time with a Start/End/Change table |
| Formatting | en-AU dates, USD, compact token numbers |
| Platform | Python standard library, Windows scheduled tasks, loopback-only server, data kept in the project folder |
| Token definition | Total = input + output + cache read + cache write |
| Cost basis | A vendor-reported figure (Claude `costUSD`, Cursor `chargedCents`) |
| Subagent hooks | Both record subagent runs through the vendor's hook mechanism |
| APIs | Both use unofficial endpoints for plan usage |

## 4. Differences

| Topic | Claude | Cursor |
|---|---|---|
| Data source | Local transcripts | Remote dashboard API |
| Refresh cost | Local file reads by byte offset (cheap) | Up to 100 HTTP pages every minute |
| Plan lookup rate | About every 10 minutes | Every minute |
| Sign-in | OAuth JSON file, refreshed by the collector | Access token read from `state.vscdb` |
| Effort | A field on each line | A suffix in the model name only |
| Cost | Per session, spread by tokens, with estimation | Per event |
| Subagent tokens | Available (sidechain lines, subagent files, hook totals) | Not available from the hook; attributable only if usage events carry the subagent conversation id |
| Storage | Sparse arrays | Dense arrays (5.8 MB) |
| Charts | Chart.js with decimation | Hand-drawn SVG, no decimation |
| Window edge | Rolling 43,200 minutes | From local midnight 29 days ago |
| Port | 8417 | 8765 |
| Embedding | `?embed=1` for the desktop pane | Canvas template |
| Plan pools | `five_hour`, `seven_day` and model-specific 7-day pools | Cursor Models, Other Models |

## 5. Risks

### Migration

- Old history lives in different forms (Claude `state.json` and `plan_samples.jsonl`, Cursor snapshot and plan history). The combined app rebuilds usage from the sources of truth (transcripts, the Cursor API) with a backfill. Old plan history is imported read-only by `collector/import_history.py` (see `docs/INSTALL.md`).
- Cursor keeps usage events for a limited time, so a 30-day backfill is the most it can recover.
- Existing hooks in `~/.claude/settings.json` and `~/.cursor/hooks.json` must be left alone; the new hooks are added beside them, with a backup first.

### Data consistency

- Claude cost depends on `cost-state` lines, and running sessions are estimated. The page states how much.
- Two de-duplication schemes (Claude message and request id, Cursor a composite key) must hold across repeated and overlapping reads. The combined app keeps both in one `seen` table.
- Cursor effort is parsed from model names, so an unexpected naming scheme would appear as part of the model name.
- Window edges differ between the sources, so a parity check shows small drift at the oldest edge.

### Performance

- Cursor fetching 30 days every minute is heavy and can reach the paging cap. Dense arrays inflate the page payload.
- Running alongside the old apps doubles Cursor and Anthropic calls.
- Unofficial APIs can change or rate limit without notice.

## 6. Reuse opportunities

| Reused from | Item | Where it went |
|---|---|---|
| Claude | Incremental transcript reader and de-duplication | `collector/adapters/claude.py` |
| Claude | Cost allocation and estimation | `collector/aggregate.py` |
| Claude | OAuth plan fetch and token refresh, 10-minute throttle | `collector/plan/claude_plan.py`, `collector/plan/__init__.py` |
| Claude | Atomic write, lock file, rotating log | `collector/util.py` |
| Claude | Chart.js setup, window and tick logic, theme variables | `web/js/charts.js`, `web/js/stats.js`, `web/css/app.css` |
| Cursor | Token lookup, endpoint fallbacks, model-name resolution | `collector/adapters/cursor.py` |
| Cursor | Plan percentage key search and fallbacks | `collector/plan/cursor_plan.py` |
| Cursor | Hook recorder | `hooks/cursor_subagent_hook.ps1` |

## 7. Outcome

The design in `docs/ARCHITECTURE.md` follows from this review. Deliberate behaviour changes against the sources are listed in `docs/CONFIGURATION.md`.
