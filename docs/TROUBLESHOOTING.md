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
| Claude plan panel empty on Linux or Windows | No `.credentials.json` in the Claude config folder. Sign in to Claude Code, or set `TOKEN_BURN_CLAUDE_TOKEN` |
| Subagents show n/a | The hook is not registered, or Cursor was not restarted after `token-burn install`. Run `token-burn status` |
| Nothing runs after reboot (Linux) | systemd user lingering is off. Run `loginctl enable-linger $USER`, or use cron |
| macOS: "cannot be opened" | Run `xattr -dr com.apple.quarantine <app folder>` |
| Windows SmartScreen warning | Unsigned build. Choose More info, then Run anyway |
| Moved the app folder or deleted the venv | The scheduler holds the old path. Run `token-burn uninstall` then `token-burn install` from the new location |
| Stale lock | A collect lock older than 5 minutes is taken over automatically |
| Database locked or corrupt | Stop the jobs and restore from backup ([DEPLOYMENT](DEPLOYMENT.md)) |

Note (Windows): scheduled tasks run `token-burnw.exe` (windowless), so no console window appears. To upgrade, replace the folder and re-run `token-burn install`.
