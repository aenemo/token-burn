# Deployment

token-burn runs per user, not as a system service. Nothing needs administrator or root rights.

## Layout

| Item | Windows | macOS | Linux |
|---|---|---|---|
| Program (zip route, recommended) | `%LOCALAPPDATA%\Programs\Token Burn` | `~/Applications/Token Burn` | `~/.local/share/token-burn` (plus a `~/.local/bin/token-burn` symlink) |
| Data (default) | `%LOCALAPPDATA%\token-burn\data` | `~/Library/Application Support/token-burn/data` | `$XDG_STATE_HOME/token-burn` or `~/.local/state/token-burn` |
| Data pointer (only for a non-default data folder) | `%LOCALAPPDATA%\token-burn\location.json` | `~/Library/Application Support/token-burn/location.json` | `$XDG_CONFIG_HOME/token-burn/location.json` or `~/.config/token-burn/location.json` |
| Scheduler | Task Scheduler | launchd | systemd user units or cron |

The installer wizard lets you choose the program and data folders (see [INSTALL](INSTALL.md)). The chosen program folder is recorded as `install_dir` in `config.json` in the data folder, so upgrades and uninstall know where it is. Use `token-burn data status` to see where the data is and `token-burn data move <folder>` to move it (refused with exit 3 if `TOKEN_BURN_DATA_DIR` is set or the target already holds `usage.db`; it copies the database with SQLite's backup API and verifies row counts before switching).

## Upgrade

Zip route: unzip the new release anywhere and run the installer again (double-click `token-burn.exe`, `Install Token Burn.command` or `./install.sh`, or run `token-burn install`). It finds the existing install and asks "Token Burn <old version> is installed in <dir>. Upgrade it to <new version>?". On yes it:

1. Stops the scheduled tasks and the server (hooks, settings and data are not touched).
2. Copies the new program to a temporary sibling folder, renames the old folder to `.old-<timestamp>`, renames the new one into place and deletes the old one. The installer writes `.token-burn-install.json` (the version and the list of files it installed) into the program folder; an upgrade or uninstall only touches the listed entries, and a folder that holds other files is never replaced wholesale. A folder is accepted as an install only with that marker and an `_internal` folder (an older install without the marker is accepted only when it holds nothing but the release files); any other non-empty folder is refused.
3. Registers the tasks again and restarts the server, then runs the Refresh task once to check it works.

If a folder is locked (for example a running `token-burn.exe` or a terminal opened in the program folder) the installer retries the rename for about 30 seconds and prints "Waiting for <dir> to be released...". If it stays locked, or the copy fails (for example the disk is full), it rolls back (the old folder is renamed back if needed), registers the previous version's tasks again, keeps the previous version installed and exits with code 1. If even the rename back fails, it says so, names the two folders to rename by hand and leaves the tasks stopped. Nothing is lost. Close the program using the folder and run the installer again.

Without a terminal, or with `--yes`: `token-burn install` upgrades in place without questions and keeps the existing sync settings (only a fresh `--yes` install is "this computer only"). There is no need to run `token-burn uninstall` first.

Pip route: `pip install --upgrade .` from the new source, then `token-burn install` to refresh the registered tasks. Data and history are kept.

## Rollback

Repeat the upgrade with the previous release, then restore the backup if the database schema changed. Keep the previous zip until the new version is confirmed.

## Backup and restore

SQLite's online backup is safe while the collector runs.

- Windows (PowerShell): `sqlite3 "$env:LOCALAPPDATA\token-burn\data\usage.db" ".backup '$env:USERPROFILE\usage-backup.db'"`
- macOS: `sqlite3 ~/Library/Application\ Support/token-burn/data/usage.db ".backup ~/usage-backup.db"`
- Linux: `sqlite3 "${XDG_STATE_HOME:-$HOME/.local/state}/token-burn/usage.db" ".backup ~/usage-backup.db"`

If `sqlite3` is not installed, run `token-burn uninstall` and copy the data folder (or use `token-burn data move`, which verifies its copy). Restore by stopping the jobs and copying the backup over `usage.db`. `usage.json` is rebuilt on the next collection.

## Security notes

- The server binds to `127.0.0.1` only and rejects non-loopback clients and unexpected `Host` headers.
- Credential policy is read-only: Claude's credentials file, the macOS Keychain item and Cursor's `state.vscdb` are read, never written or refreshed; opencode's `opencode.db` is opened read-only and its credential tables are never read.
- `TOKEN_BURN_CLAUDE_TOKEN` in a user environment variable is stored in clear text by the OS. Prefer the keyring extra or the vendor's own credentials.
- Do not expose the port with a proxy or tunnel. There is no authentication.

Note (Windows): scheduled tasks run `token-burnw.exe` (windowless), so no console window appears. To upgrade, run the installer from the new release.
