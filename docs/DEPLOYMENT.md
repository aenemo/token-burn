# Deployment

ccu runs per user, not as a system service. Nothing needs administrator or root rights.

## Layout

| Item | Windows | macOS | Linux |
|---|---|---|---|
| App (zip route) | `%LOCALAPPDATA%\ccu\app` | `~/Library/Application Support/ccu/app` | `~/.local/share/ccu/app` |
| Data | `%LOCALAPPDATA%\ccu\data` | `~/Library/Application Support/ccu/data` | `$XDG_STATE_HOME/ccu` or `~/.local/state/ccu` |
| Scheduler | Task Scheduler | launchd | systemd user units or cron |

## Upgrade

1. Back up the data directory (below).
2. `ccu uninstall`.
3. Zip route: replace the app folder with the new release. Pip route: `pip install --upgrade .` from the new source.
4. `ccu install`. Data and history are kept.

## Rollback

Repeat the upgrade with the previous release, then restore the backup if the database schema changed. Keep the previous zip until the new version is confirmed.

## Backup and restore

SQLite's online backup is safe while the collector runs.

- Windows (PowerShell): `sqlite3 "$env:LOCALAPPDATA\ccu\data\usage.db" ".backup '$env:USERPROFILE\usage-backup.db'"`
- macOS: `sqlite3 ~/Library/Application\ Support/ccu/data/usage.db ".backup ~/usage-backup.db"`
- Linux: `sqlite3 "${XDG_STATE_HOME:-$HOME/.local/state}/ccu/usage.db" ".backup ~/usage-backup.db"`

If `sqlite3` is not installed, run `ccu uninstall` and copy the data folder. Restore by stopping the jobs and copying the backup over `usage.db`. `usage.json` is rebuilt on the next collection.

## Security notes

- The server binds to `127.0.0.1` only and rejects non-loopback clients and unexpected `Host` headers.
- Credential policy is read-only: Claude's credentials file, the macOS Keychain item and Cursor's `state.vscdb` are read, never written or refreshed.
- `CCU_CLAUDE_TOKEN` in a user environment variable is stored in clear text by the OS. Prefer the keyring extra or the vendor's own credentials.
- Do not expose the port with a proxy or tunnel. There is no authentication.

Note (Windows): scheduled tasks run `ccuw.exe` (windowless), so no console window appears. To upgrade, replace the folder and re-run `ccu install`.
