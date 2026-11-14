# Token Burn

Version 0.9.1.

Token Burn (`token-burn`) collects token usage and costs for Claude, Cursor, Opencode and OpenAI and shows them on one local dashboard, side by side, in that order from the left. It runs on Windows, macOS and Linux, uses only the Python standard library at runtime, and keeps everything on your machine.

The default port is 8421; override with TOKEN_BURN_PORT or token-burn serve --port.

## What you get

- A small header animation: data blocks burn up as they travel along the title. It is decorative, pauses when the tab is hidden and respects the reduced-motion setting.
- One page at `http://127.0.0.1:8421/` with a shared window selector: 30m, 1h, 6h, 12h (default), 24h, 7d and 30d.
- On the Claude and opencode columns, an Agents switch (All is the default, or Split) separates each model's line into primary agent (solid) and subagent (dashed) lines in the same colour. The Total line, tables and tiles are unaffected. The Claude column's summary row also shows Primary agent, Subagents and Cache hit rate tiles for the selected window; Cache hit rate is cache read divided by (input + cache read + cache write), with the cache read tokens underneath, and is hidden when usage.json has no `cache_read` data. The Cursor column's summary row also shows a Cache hit rate tile, calculated the same way. Every column with token counts also shows an **Input : output** tile: input-side tokens (fresh input plus cache read and write) divided by output tokens, with both counts underneath and the full figures on hover. Each column has a **Value and limits** card. **Value for money** shows the cost of the last 30 days (or the history there is) at that platform's prices: API list prices for Claude, what Cursor charged for Cursor, and what opencode reported for opencode. Type your monthly plan fee in US$ and it shows how many times the fee that usage would have cost per token, with the fee pro-rated to the same number of days. The fee is kept in this browser only. **Limit forecast** projects each plan pool to 100% from its recent pace (the last hour for the 5-hour pool, the last 24 hours for the others, counting only samples since the last reset) and says whether it will hit the limit before it resets, or what it will be at when it resets. It needs at least 10 minutes of samples.
- A graph mode switch on each Usage graph: Cumulative (the default) or Rolling total. Rolling total draws a line per model and effort plus a bold Total line, summing usage over the last 30 min, 1 hour, 6 hours, 12 hours, 24 hours (default), 7 days or 30 days, chosen with the "Total over the last" picker. History is limited to 30 days, so early points on long periods sum less than a full period (the page says so). Selecting a model chip highlights its line and shows a callout over the chart with that model's usage for the selected time period.
- A **Sessions** card under each Usage graph: one bar per session active in the selected window, split into model colours, following the window selector and the Tokens/Cost switch. It shows the top 15 sessions plus an "Other" bar; an **All sessions** table above the Subagents table lists every session (models, tokens, cost, last active). Clicking a model segment or button highlights that model everywhere in the column. Session names come from Claude's automatic or `/rename` titles, opencode's session titles and, for Cursor, chat and cloud agent names from this computer's Cursor app. Cursor agent runs started by other tools have no name, so they are labelled with their project folder, for example "Agent 056449 · RAG installer" (on this computer only, never synced). Other sessions show a short ID. Prompt text is never read.
- Tokens or cost, per model and effort, with a statistics table and a subagents table.
- Header toggle buttons turn each provider's column on or off live. Drag a button left or right (or focus it and press Alt with an arrow key) to move its column to the same place; the order is remembered in the browser. A **Columns across** selector (1 to 6, default 4) sets how many columns sit side by side before the next row starts, limited to what fits the window. The visible columns share the page width, the choice is remembered in the browser (`token-burn.providers`) and a provider that is not installed is disabled.
- Provider columns from opencode: requests that opencode records for Google, DeepSeek, NVIDIA, Mistral, Qwen, Meta, GitHub Copilot, xAI Grok and Cloudflare Workers AI models get their own column (and header button) instead of counting as opencode. A column appears only once that provider has usage in the last 30 days, so providers you never use never show. Costs are what opencode reported. Standalone tools that keep local session files are read too and add to the same column: Gemini CLI (Google), Qwen Code (Qwen), GitHub Copilot CLI (GitHub Copilot), Grok Build CLI (xAI Grok) and Mistral Vibe (Mistral). They record no cost, so only tokens are shown for them. Grok Bot on X, Telegram or Discord is a hosted service that keeps nothing on this computer, so it cannot be tracked. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- opencode column: usage is read from opencode's local SQLite database (see [docs/CONFIGURATION.md](docs/CONFIGURATION.md)). Model labels read provider/model, effort is the model variant, reasoning tokens count as output, and child sessions appear as subagents (the Agents All/Split switch works). It has a Cache hit rate tile. opencode records cost 0 for free or gateway models, so cost can read US$0.00 (the page says so); rely on token use. opencode has no plan quota, so its plan card says there is no plan data. Totals match opencode's own session totals to within about 0.4 percent.
- Plan usage history for Claude and Cursor.
- A collector that runs every minute for tokens and refreshes plan data about every 10 minutes.
- Optional use on several computers: each computer writes a small summary file into a folder your sync app copies, and every dashboard combines them (see below).
- Data is stored in SQLite under a per-user data directory, with 30 days of retention.

## Install

Two routes. Full steps are in [docs/INSTALL.md](docs/INSTALL.md).

1. **Zip (no Python needed).** Download the zip for your OS from the GitHub Release, unzip it anywhere and double-click `token-burn.exe` (Windows), `Install Token Burn.command` (macOS) or run `install.sh` (Linux). A wizard asks where to install, where to keep the data and whether to share across computers.
2. **From source.** Needs Python 3.9 or later.

```
pip install .
token-burn install
```

Add the optional keyring credential source with `pip install ".[keyring]"`.

## Use on several computers

The installer asks whether to use Token Burn on more than one computer. Choose a folder that your own sync app (OneDrive, Dropbox, iCloud Drive, Google Drive) or a network share already copies, and each computer writes a small summary file there. Every dashboard then shows the combined Claude and opencode usage, with a toggle row to filter by computer. Cursor usage is account-wide, so it is not added up. Only token counts, costs, model names and the computer's name and operating system are shared, never prompts, code or credentials. The live data folder is never synced. Full guide: [docs/SYNC.md](docs/SYNC.md).

## Commands

| Command | Purpose |
|---|---|
| `token-burn install` | Run the install wizard (or use flags such as `--install-dir`, `--sync-folder`, `--yes`): register the scheduler jobs and the Claude and Cursor hooks (opencode needs none), then start the dashboard |
| `token-burn uninstall` | Remove the jobs, hooks and shortcuts (data and shared-folder history are kept unless you ask with `--purge-data` or `--remove-from-shared-folder`; `--yes` also deletes the program folder without asking) |
| `token-burn sync ...` | Manage use on several computers: `status`, `enable`, `disable`, `machines`, `rename`, `retire`, `unretire`, `remove`, `reset-id`, `now` |
| `token-burn data ...` | `status` shows the data folder, `move <folder>` relocates it (`--yes` also deletes the old copy) |
| `token-burn status` | Show what is registered and whether the server is up |
| `token-burn serve` | Run the dashboard server in the foreground (`--port` overrides the port) |
| `token-burn collect` | Run one collection pass (`--loop` repeats, `--backfill` re-reads the full 30 days) |
| `token-burn hook` | Used by the Claude and Cursor hooks to record subagent usage |

## Documentation

- [Install](docs/INSTALL.md)
- [Use on several computers](docs/SYNC.md)
- [Deployment, upgrade, rollback and backup](docs/DEPLOYMENT.md)
- [Configuration](docs/CONFIGURATION.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Architecture](docs/ARCHITECTURE.md) and [data contract](docs/DATA-CONTRACT.md)

## Security

The server listens on loopback only and checks the `Host` header. Credentials for Claude and Cursor are read, never modified, and opencode's database is opened read-only (its credential tables are never read). Nothing is sent anywhere except the vendors' own usage and plan endpoints, which are unofficial and may change.

## Development

```
python -m unittest discover tests
python packaging/build.py     # PyInstaller onedir plus zip in dist/
```
