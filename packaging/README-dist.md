# token-burn

1. Unzip this folder somewhere permanent and per-user, for example:
   - Windows: `%LOCALAPPDATA%\Programs\token-burn`
   - macOS: `~/Library/Application Support/token-burn/app`
   - Linux: `~/.local/share/token-burn/app`
2. Open a terminal in the folder and run `token-burn install`. It registers the scheduler, the Claude and Cursor hooks and starts the dashboard at http://127.0.0.1:8421/.
3. Remove everything with `token-burn uninstall` (your data folder is kept unless you delete it).

Upgrade: run `token-burn uninstall`, replace the folder with the new release, then run `token-burn install` again. Data is kept.

macOS unsigned builds: run `xattr -d com.apple.quarantine token-burn` and `xattr -dr com.apple.quarantine .` in the folder before first use.

Windows: SmartScreen may warn about an unsigned download. Choose More info, then Run anyway, or unblock the zip (Properties, Unblock) before extracting.

Windows: `token-burn.exe` is the console program (used by the hooks). The scheduled tasks use `token-burnw.exe`, a windowless twin in the same folder, so no console window appears every minute. To upgrade, replace the folder and re-run `token-burn install`.
