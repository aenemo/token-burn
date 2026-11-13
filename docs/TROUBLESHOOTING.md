# Troubleshooting

Start with `token-burn status`. Logs are in the data directory (see [CONFIGURATION](CONFIGURATION.md)).

| Symptom | Cause and fix |
|---|---|
| Page does not load | Server job not running. Run `token-burn status`, then `token-burn install` or `token-burn serve`. Check the port is free, or use `--port` |
| 403 from the server | The request is not from loopback, or the `Host` header is not `127.0.0.1`, `localhost` or `[::1]` with the port. Use `http://127.0.0.1:8421/` |
| Page loads but charts are empty | No data yet. Wait a minute for the first collection, or run `token-burn collect`. Check `CLAUDE_CONFIG_DIR` and `CURSOR_STATE_DB` |
| Cursor shows no data | Cursor must be signed in. Confirm the `state.vscdb` path for your OS or set `CURSOR_STATE_DB`. Cursor's endpoints are unofficial and may have changed |
| Cursor shows "usage unavailable (status 401)" or "token is encrypted" | Newer Cursor builds store the sign-in token encrypted (`cursor-safe-storage`). On Windows token-burn decrypts it in memory for the same Windows user, so it must run as the user who signs in to Cursor. On macOS and Linux decryption is not supported yet. A plain 401 with a decryptable token means Cursor is signed out: sign in again |
| opencode shows no data | opencode must have created `~/.local/share/opencode/opencode.db` (or `$XDG_DATA_HOME/opencode/opencode.db`); set `TOKEN_BURN_OPENCODE_DB` if elsewhere. The status "unsupported opencode data format" means the database is not the v2 layout; "opencode database busy, will retry" clears by itself |
| opencode cost shows US$0.00 | opencode records cost 0 for free or gateway models. Use token use instead. opencode has no plan data either |
| Claude plan panel empty on macOS | Keychain access was denied. Re-run `token-burn collect` and allow access, or set `TOKEN_BURN_CLAUDE_TOKEN` |
| Claude plan panel says "sign-in expired, run claude once" | The keep-alive normally prevents this. Check `claude` is on the PATH (or set `TOKEN_BURN_CLAUDE_BIN`) and that `TOKEN_BURN_CLAUDE_KEEPALIVE` is not 0. If the sign-in was revoked, run `claude` once and sign in |
| Claude plan panel empty on Linux or Windows | No `.credentials.json` in the Claude config folder. Sign in to Claude Code, or set `TOKEN_BURN_CLAUDE_TOKEN` |
| Subagents show n/a | The hook is not registered, or Cursor was not restarted after `token-burn install`. Run `token-burn status` |
| Nothing runs after reboot (Linux) | systemd user lingering is off. Run `loginctl enable-linger $USER`, or use cron |
| macOS: "cannot be opened" | Run `xattr -dr com.apple.quarantine <app folder>` |
| Windows SmartScreen warning | Unsigned build. Choose More info, then Run anyway |
| Moved the app folder or deleted the venv | The scheduler holds the old path. Run `token-burn uninstall` then run the installer again from the new location (zip installs: the wizard offers to repair) |
| Stale lock | A collect lock older than 5 minutes is taken over automatically |
| Install wizard says it is running from a temporary folder | You ran Token Burn from inside the zip viewer or a temp folder. Unzip the whole folder first (Desktop or Documents is fine), then run it again from there |
| Locked files during an upgrade (Windows) | A running `token-burn.exe` or `token-burnw.exe`, or a terminal opened in the program folder, blocks the replace. The installer prints "Waiting for <dir> to be released..." and retries for about 30 seconds, then rolls back, keeps the previous version installed and exits with code 1. Close the program (Task Manager, or end the Token Burn dashboard server) and run the installer again. The upgrade stops the scheduled tasks itself, so you rarely need to |
| "Scheduler check failed" after install in a custom folder | Windows Task Scheduler could not start the exe from that folder (error 0x80070002). Accept the offer to move to the recommended folder (`%LOCALAPPDATA%\Programs\Token Burn`), or run the wizard again and choose it |
| macOS: "Install Token Burn.command cannot be opened" (Gatekeeper) | The download is unsigned. Right-click the file, choose Open, then Open. Or open System Settings, Privacy and Security and choose Open Anyway. The script then removes the quarantine flag from the folder. If it still fails run `xattr -dr com.apple.quarantine .` in the folder |
| Windows SmartScreen on `token-burn.exe` | Unsigned build. Choose More info, then Run anyway. Unblocking the zip before extracting (Properties, Unblock) avoids it |
| Sync: "shared folder not found: <folder>" | The folder in `config.json` is missing. Your sync app may be signed out or paused, a drive may be unplugged, or the folder was moved. This computer keeps showing its own data. Fix the folder or run `token-burn sync enable --folder <path>` |
| Sync: a computer shows as stale | It has not written for over an hour: it is off, asleep, offline or its sync app is paused. Idle computers that are running refresh every 30 minutes. If it is gone for good, run `token-burn sync retire "<name>"` (keeps history) or `token-burn sync remove "<name>"` |
| Sync: "Another computer (hostname X) is writing with this computer's ID" | Two computers share an id, usually a cloned VM or a copied data folder. Run `token-burn sync reset-id` on one of them. Until then that computer stops exporting but still shows the others |
| Sync: "not downloaded yet" | The other computer's file is an online-only placeholder (OneDrive Files On-Demand, iCloud) or has not synced yet. Wait for the sync app, or set the `token-burn` folder to always keep on this device (OneDrive: right-click, Always keep on this device; iCloud: Keep Downloaded). The last good copy is shown meanwhile |
| Sync: "incomplete or damaged (still syncing?)" | A file was read part-way through a sync. It recovers by itself and the last good copy is shown meanwhile |
| Sync: files with odd names in `machines/` (conflict copies) | Your sync app created copies such as `x (1).json.gz`. Token Burn ignores anything that is not exactly `<id>.json.gz` and counts them as ignored files. They are safe to delete |
| Sync: work OneDrive blocked, or missing files | Your employer's IT policy may restrict or monitor OneDrive. Ask IT, or choose a personal synced folder or a network share. Only counts, costs, model names and computer names are stored there |
| Sync: "made by a newer Token Burn; update this computer" | A peer wrote a newer file format. Upgrade this computer |
| Sync: "This computer was removed from the shared folder" | Another computer ran `sync remove` for this one. Run `token-burn sync enable` to rejoin with a new id |
| Sync: Cursor totals look too low or not added up | Intended. Cursor usage is account-wide, so the larger value per minute is used rather than a sum |
| Data folder refused (synced, network or inside the program folder) | The live database must stay on a local, unsynced folder. Choose a folder such as the recommended default, and use the shared folder step to combine computers |
| `This needs confirmation. Run it in a terminal, or add --yes.` (exit 3) | `sync remove`, `sync disable --remove-my-data` and `sync reset-id` ask first, and are refused when there is no terminal. Run them in a terminal, or add `--yes` |
| `TOKEN_BURN_DATA_DIR is set, so the data folder cannot be changed/moved` (exit 3) | The variable fixes the data folder, so `install --data-dir` and `data move` refuse. Remove the variable first |
| `That folder already holds Token Burn data` from `data move` (exit 3) | The target already has `usage.db`. Choose an empty folder |
| Database locked or corrupt | Stop the jobs and restore from backup ([DEPLOYMENT](DEPLOYMENT.md)) |

Note (Windows): scheduled tasks run `token-burnw.exe` (windowless), so no console window appears. To upgrade, run the installer from the new release; it replaces the files for you.
