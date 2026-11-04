# Troubleshooting

Start with `ccu status`. Logs are in the data directory (see [CONFIGURATION](CONFIGURATION.md)).

| Symptom | Cause and fix |
|---|---|
| Page does not load | Server job not running. Run `ccu status`, then `ccu install` or `ccu serve`. Check the port is free, or use `--port` |
| 403 from the server | The request is not from loopback, or the `Host` header is not `127.0.0.1`, `localhost` or `[::1]` with the port. Use `http://127.0.0.1:8421/` |
| Page loads but charts are empty | No data yet. Wait a minute for the first collection, or run `ccu collect`. Check `CLAUDE_CONFIG_DIR` and `CURSOR_STATE_DB` |
| Cursor shows no data | Cursor must be signed in. Confirm the `state.vscdb` path for your OS or set `CURSOR_STATE_DB`. Cursor's endpoints are unofficial and may have changed |
| Claude plan panel empty on macOS | Keychain access was denied. Re-run `ccu collect` and allow access, or set `CCU_CLAUDE_TOKEN` |
| Claude plan panel empty on Linux or Windows | No `.credentials.json` in the Claude config folder. Sign in to Claude Code, or set `CCU_CLAUDE_TOKEN` |
| Subagents show n/a | The hook is not registered, or Cursor was not restarted after `ccu install`. Run `ccu status` |
| Nothing runs after reboot (Linux) | systemd user lingering is off. Run `loginctl enable-linger $USER`, or use cron |
| macOS: "cannot be opened" | Run `xattr -dr com.apple.quarantine <app folder>` |
| Windows SmartScreen warning | Unsigned build. Choose More info, then Run anyway |
| Moved the app folder or deleted the venv | The scheduler holds the old path. Run `ccu uninstall` then `ccu install` from the new location |
| Stale lock | A collect lock older than 5 minutes is taken over automatically |
| Database locked or corrupt | Stop the jobs and restore from backup ([DEPLOYMENT](DEPLOYMENT.md)) |

Note (Windows): scheduled tasks run `ccuw.exe` (windowless), so no console window appears. To upgrade, replace the folder and re-run `ccu install`.
