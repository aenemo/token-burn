# Token Burn

1. Unzip this folder anywhere (not from inside the zip viewer).
2. Start the installer:
   - Windows: double-click `token-burn.exe`.
   - macOS: double-click `Install Token Burn.command` (first time: right-click, Open, Open).
   - Linux: run `./install.sh`.
3. The wizard asks where to install Token Burn (it suggests a per-user folder; you can unzip anywhere and it copies itself there), where to keep its data, and whether to share usage across your computers. When it finishes it opens the dashboard at http://127.0.0.1:8421/.

To use Token Burn on several computers, run the installer on each one, choose "Start a new shared folder" on the first and "Join an existing shared folder" on the others, and pick the same synced folder (OneDrive, Dropbox, iCloud Drive, Google Drive or a network share). Only token counts, costs, model names and the computer's name and OS are shared.

Upgrade: unzip the new release anywhere and run the installer again. It finds the existing install, stops the background tasks, replaces the files, restarts them and keeps your data and settings. No uninstall is needed. If a file is locked it waits about 30 seconds ("Waiting for <dir> to be released..."), then rolls back, keeps the previous version and exits with code 1: close the program using the folder and run the installer again.

Uninstall: run `token-burn uninstall` (on Windows also Settings, Apps, Installed apps, Token Burn). It asks whether to delete the program folder; `--yes` deletes it without asking. Your data folder and shared-folder history are kept unless you add `--purge-data` or `--remove-from-shared-folder`.

Command line (every OS): `token-burn install --install-dir PATH --sync-folder PATH --new --machine-name NAME --yes` runs without questions. Other flags: `--data-dir`, `--join`, `--no-sync`, `--adopt NAME`, `--no-open`, `--no-start`. The sharing flags (`--new`, `--join`, `--machine-name`, `--adopt`) need `--sync-folder`. Exit codes: 0 ok, 1 error, 2 bad arguments, 3 refused or cancelled.

macOS: the launcher removes the download quarantine flag from this folder. If macOS still refuses to open it, use System Settings, Privacy and Security, Open Anyway, or run `xattr -dr com.apple.quarantine .` in the folder.

Windows: SmartScreen may warn about an unsigned download. Choose More info, then Run anyway, or unblock the zip (Properties, Unblock) before extracting. `token-burn.exe` is the console program (used by the hooks). The scheduled tasks use `token-burnw.exe`, a windowless twin in the same folder, so no console window appears every minute.

More: docs/INSTALL.md, docs/SYNC.md and docs/TROUBLESHOOTING.md in the project.
