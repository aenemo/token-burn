# Install

Supported: Windows 10 or later, macOS 12 or later, and Linux with systemd (cron is used where systemd user units are unavailable). Claude Code, Cursor and opencode need to be used on the same machine for their data to appear.

## Route 1: PyInstaller zip (no Python needed)

1. Download `token-burn-<version>-<OS>.zip` and `SHA256SUMS` from the GitHub Release. Verify the checksum.
   - Windows: `certutil -hashfile token-burn-v0.3.1-Windows.zip SHA256`
   - macOS: `shasum -a 256 token-burn-v0.3.1-macOS.zip`
   - Linux: `sha256sum -c SHA256SUMS --ignore-missing`
2. Unzip the whole folder anywhere (your Desktop or Documents is fine, but not straight from inside the zip viewer).
3. Start the installer:
   - Windows: double-click `token-burn.exe`. A console window opens and the wizard runs. SmartScreen may warn: choose More info, then Run anyway.
   - macOS: double-click `Install Token Burn.command`. The first time, right-click it and choose Open, then Open again (or use System Settings, Privacy and Security, Open Anyway). The script removes the download quarantine flag for the folder and runs the wizard.
   - Linux: run `./install.sh` (or `./token-burn install`) from the folder.
4. Answer the wizard (see below). When it finishes it opens `http://127.0.0.1:8421/`.

The wizard copies the program to its install location, so the unzipped folder can be deleted afterwards. If you run Token Burn from a temporary folder (for example straight from inside the zip) it stops and asks you to unzip first.

## What the wizard asks

1. **Where to install Token Burn.** Per-user locations that need no administrator rights:

   | OS | Recommended program folder |
   |---|---|
   | Windows | `%LOCALAPPDATA%\Programs\Token Burn` |
   | macOS | `~/Applications/Token Burn` |
   | Linux | `~/.local/share/token-burn` (honours `XDG_DATA_HOME`), plus a symlink `~/.local/bin/token-burn` |

   The first screen lists the program and data folders and asks "Press Enter to accept, or type C to change them". If you type C, press Enter for the recommended folder, type B to browse, or paste a path. Temporary folders, Downloads, synced folders (OneDrive, Dropbox, iCloud Drive, Google Drive), network folders, folders needing administrator rights and non-empty folders that are not a Token Burn install are refused.
2. **Where to keep the data.** Defaults: Windows `%LOCALAPPDATA%\token-burn\data`, macOS `~/Library/Application Support/token-burn/data`, Linux `$XDG_STATE_HOME/token-burn` or `~/.local/state/token-burn`. Type C at the "Token Burn will use these folders" screen to change the program or data folder. The data folder must be writable, local (not synced, not a network share, not a temporary folder), outside the program folder, and either empty or already holding Token Burn data (`usage.db` or `config.json`). If `TOKEN_BURN_DATA_DIR` is set the data folder is fixed: the screen shows it as set by the variable and cannot change it. A non-default choice is recorded in `location.json` (see [CONFIGURATION](CONFIGURATION.md)).
3. **Whether to share across computers.** "This computer only", "Start a new shared folder" or "Join an existing shared folder". See [SYNC](SYNC.md).
4. **A name for this computer**, if sharing.
5. A confirmation screen ("Install now?"), then the install: hooks, scheduled tasks, the server and a health check. Token Burn then runs the scheduled collector task once and checks it succeeded. With `--no-start` the server is not started and the scheduler check is skipped too.

If Token Burn is already installed the wizard says so and offers to keep and repair, change the sync settings, change the data folder, or cancel.

### Upgrades

Run the wizard from the newer release. If an older Token Burn is installed it says "Token Burn <old version> is installed in <dir>. Upgrade it to <new version>?". If the installed version is the same it asks "Replace it with this copy?" instead. Upgrading stops the scheduled tasks and the server, replaces the program files, re-registers the tasks and restarts. Hooks, settings and data are kept. If a folder is locked (for example a running `token-burn.exe`, or a terminal opened in the program folder) the installer retries the rename for about 30 seconds and prints "Waiting for <dir> to be released...". If it stays locked it rolls back, registers the previous version's tasks and server again, keeps the previous version installed and exits with code 1. Close the program using the folder and run the installer again. There is no need to uninstall, unzip over the old folder or reinstall by hand.

### Operating system integration

On Windows a Start Menu entry "Token Burn" opens the dashboard, and Token Burn appears under Settings, Apps, Installed apps with an Uninstall button. Both are per user and are removed on uninstall. On Linux a `token-burn.desktop` entry is added under `~/.local/share/applications`. macOS gets only the folder.

### Non-interactive install

The wizard only runs in a terminal, and only when none of `--yes`, `--sync-folder` or `--no-sync` is given. With those flags, or without a terminal, it never prompts.

```
token-burn install --install-dir PATH --sync-folder PATH --new --machine-name "Office PC" --yes
```

| Flag | Meaning |
|---|---|
| `--install-dir PATH` | Program folder (zip builds). Without this flag and without a terminal, an existing install's folder is reused, otherwise the recommended folder is used, even without `--yes` |
| `--data-dir PATH` | Data folder (the same rules as the wizard apply; see [CONFIGURATION](CONFIGURATION.md)). If `TOKEN_BURN_DATA_DIR` is set it is refused with exit code 3 (unless it names the same folder) |
| `--sync-folder PATH` | Shared folder (a synced folder; Token Burn creates `token-burn` inside it, and creates `PATH` if it is missing). If it does not look synced, a note is printed and the install continues |
| `--new` / `--join` | Start a new shared folder, or join an existing one. Cannot be combined with each other. Without either, it joins when the folder already holds Token Burn files, otherwise it starts a new one. `--join` into a folder with no Token Burn files exits 3 |
| `--no-sync` | This computer only. Cannot be combined with any other sync option (exit 2) |
| `--machine-name NAME` | This computer's name (1 to 40 characters, unique; a clash exits 3) |
| `--adopt NAME` | Take over an existing computer's identity in the shared folder (reinstall). It implies join and cannot be combined with `--new` (exit 2). It exits 3 if the name is not found, or if this computer already has an identity (run `token-burn sync reset-id` first) |
| `--yes` | Accept defaults and confirmations (no questions). On a fresh install with no sync flags it means "this computer only". On an existing install it keeps that install's sync settings |
| `--no-open` | Do not open the browser at the end |
| `--no-start` | Register everything but do not start the server. This also skips the scheduler check |

`--new`, `--join`, `--machine-name` and `--adopt` all need `--sync-folder`; without it the command exits 2.

Exit codes: 0 ok, 1 error (including a name or id that matches no computer), 2 bad arguments, 3 refused or cancelled (duplicate name, conflict, cancelled, temporary folder).

## Route 2: pip from source

```
git clone https://github.com/aenemo/token-burn.git token-burn
cd token-burn
python -m venv .venv
.venv/bin/pip install .          # Windows: .venv\Scripts\pip install .
.venv/bin/token-burn install            # Windows: .venv\Scripts\token-burn install
```

Add the `keyring` extra with `pip install ".[keyring]"`. The scheduler records the absolute path of the interpreter, so do not delete the virtual environment while installed. pip installs skip the program-folder step (pip already placed the code) but still ask about the data folder and sharing.

## What `token-burn install` does

| OS | Scheduler |
|---|---|
| Windows | Task Scheduler jobs for the collector (every minute) and the server (at log on) |
| macOS | launchd agents in `~/Library/LaunchAgents` |
| Linux | systemd user timer and service, or a cron entry if systemd user units are unavailable |

It also registers the Claude and Cursor subagent hooks (opencode needs no hook; it is read from its own database). Use `token-burn status` to check. The default port is 8421 so it can run beside the original app on 8420; override with TOKEN_BURN_PORT or token-burn serve --port.

On macOS, plan data for Claude comes from the Keychain item "Claude Code-credentials". The first read may show an access prompt. Without it, set `TOKEN_BURN_CLAUDE_TOKEN`.

## Uninstall

`token-burn uninstall` removes the scheduled tasks, hooks, Start Menu entry, Apps entry, desktop entry, and the `~/.local/bin/token-burn` symlink (only when it points into the install folder), then in a terminal asks "Also delete the program folder <dir>?" (default yes). Only the files Token Burn installed are deleted (they are listed in `.token-burn-install.json` in the program folder); anything else you put there is left alone, and the folder is removed only when nothing else is in it. On Windows a running exe cannot delete itself, so a small background command waits (up to about a minute) for the uninstall to finish and then deletes the files; from Apps and features that happens after you press Enter at the end.

Options:

| Flag | Meaning |
|---|---|
| `--yes` | Ask nothing; also delete the program folder |
| `--interactive` | Ask the questions and wait for Enter at the end. Used by the Windows Apps and features entry |
| `--purge-data` | Also delete the data folder |
| `--remove-from-shared-folder` | Also delete this computer's history from the shared folder |

Without a terminal (and without `--interactive`) uninstall asks nothing: it keeps the program folder unless you pass `--yes`, and `--purge-data` deletes the data folder without confirmation. Exit codes are the same as for install.

Your data and your shared-folder history are kept by default:

- `--purge-data` also deletes Token Burn's files in the data folder (asked first in a terminal; with `--yes` or without a terminal it deletes without asking), including this computer's id. Files that are not Token Burn's are left in place, and the folder is removed only when it ends up empty. Only a purge forgets a custom data folder (`location.json`): without `--purge-data` the pointer is kept, so a reinstall finds the same data and the same computer id.
- Your history in the shared folder is kept so other computers still show it. To remove it, run `token-burn sync remove "<name>"` on any computer, or add `--remove-from-shared-folder` to the uninstall.

Note (Windows): scheduled tasks run `token-burnw.exe` (windowless), so no console window appears.
