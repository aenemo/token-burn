# Install

Supported: Windows 10 or later, macOS 12 or later, and Linux with systemd (cron is used where systemd user units are unavailable). Claude Code, Cursor and opencode need to be used on the same machine for their data to appear.

## Route 1: PyInstaller zip

1. Download `token-burn-<version>-<OS>.zip` and `SHA256SUMS` from the GitHub Release. Verify the checksum.
   - Windows: `certutil -hashfile token-burn-v0.1.0-Windows.zip SHA256`
   - macOS: `shasum -a 256 token-burn-v0.1.0-macOS.zip`
   - Linux: `sha256sum -c SHA256SUMS --ignore-missing`
2. Unzip into a per-user folder that will not move:
   - Windows: `%LOCALAPPDATA%\Programs\token-burn`
     (Use this folder on Windows. On the test machine, Task Scheduler could not launch the exe from other
     folders under `%LOCALAPPDATA%`, failing with error 0x80070002, but `Programs` worked.)
   - macOS: `~/Library/Application Support/token-burn/app`
   - Linux: `~/.local/share/token-burn/app`
3. Run `token-burn install` from that folder.
   - macOS unsigned builds: run `xattr -dr com.apple.quarantine .` in the folder first.
   - Windows: SmartScreen may warn. Choose More info, then Run anyway.
4. Open `http://127.0.0.1:8421/`.

## Route 2: pip from source

```
git clone <repository-url> token-burn
cd token-burn
python -m venv .venv
.venv/bin/pip install .          # Windows: .venv\Scripts\pip install .
.venv/bin/token-burn install            # Windows: .venv\Scripts\token-burn install
```

Add the `keyring` extra with `pip install ".[keyring]"`. The scheduler records the absolute path of the interpreter, so do not delete the virtual environment while installed.

## What `token-burn install` does

| OS | Scheduler |
|---|---|
| Windows | Task Scheduler jobs for the collector (every minute) and the server (at log on) |
| macOS | launchd agents in `~/Library/LaunchAgents` |
| Linux | systemd user timer and service, or a cron entry if systemd user units are unavailable |

It also registers the Claude and Cursor subagent hooks (opencode needs no hook; it is read from its own database). Use `token-burn status` to check. The default port is 8421 so it can run beside the original app on 8420; override with TOKEN_BURN_PORT or token-burn serve --port.

On macOS, plan data for Claude comes from the Keychain item "Claude Code-credentials". The first read may show an access prompt. Without it, set `TOKEN_BURN_CLAUDE_TOKEN`.

## Uninstall

`token-burn uninstall` removes jobs and hooks. Delete the data directory (see [CONFIGURATION](CONFIGURATION.md)) and the app folder to remove everything.

Note (Windows): scheduled tasks run `token-burnw.exe` (windowless), so no console window appears. To upgrade, replace the folder and re-run `token-burn install`.
