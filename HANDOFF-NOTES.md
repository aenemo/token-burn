## Goal
Make the Claude and Cursor Combined Token Usage Graph OS agnostic (Windows, macOS, Linux) with full feature parity. Source folder `C:\AI-Coding\Personal\Claude and Cursor Combined Token Usage Graph` is READ-ONLY. All work goes in this folder.

## Done
- Planning only. Four read-only Opus analysis agents ran (architecture, cross-platform and dependencies, deployment, security). Full 10-section plan was presented in chat.
- No code, build or project files created. Not verified: nothing was run beyond a directory listing.

## Decisions
- Docker is dropped from scope (user decision). Do not plan Docker work.
- The PowerShell Cursor hook is replaced entirely by a Python hook (user decision), no Windows shim.
- Baseline is native Python with a single `ccu` CLI (`install|uninstall|serve|collect|hook`) and per-OS scheduler back ends (Task Scheduler, launchd, systemd --user).
- Distribution via PyInstaller `--onedir` zip per OS (no cross-compiling). Plan answered that this is deployable by unzip then `ccu install`. Signing and installers (.msi/.pkg/.deb) still undecided.

## Open questions / blockers
- Awaiting explicit approval to start implementation planning and project creation here.
- Whether to stop at the unsigned PyInstaller zip or add signing and notarisation (V3).
- Whether macOS plan data via Keychain read is acceptable as the only macOS path.

## Parity must not change
- Claude left, Cursor right (`web/index.html:31-32`, `app.js:364`, `app.css:75-78`).
- Filters 30m/1h/6h/12h/24h/7d/30d (`web/js/stats.js:6-14`), default 12h.
- Token refresh 1 min (`util.py:18` TOKEN_REFRESH_S=60, `app.js:9` REFRESH_MS=60000, scheduler tick in `Setup.ps1:209`).
- Plan refresh 10 min (`util.py:19` PLAN_INTERVAL_S=590, throttle in `collector/plan/__init__.py`).

## Next action
On approval, create the project skeleton here: copy (not move) the source tree, add `.gitattributes`, then start WS1 (platform layer: `cursor_state_db()` per OS at `collector/util.py:176-179`).

## Next steps
1. WS0 baseline and parity harness (159 existing tests plus JS golden tests).
2. WS1 platform layer and credential resolver (env, keyring, vendor file).
3. WS2 Python Cursor hook and hook registrar.
4. WS3 installer and scheduler back ends.
5. WS4 server hardening (Host allowlist, CSP).
6. WS5 frontend reads intervals from usage.json, defaults unchanged.
7. WS6 CI matrix and PyInstaller zips. WS7 docs.

## 4/11/2026: opencode support documented
The dashboard now has a third column for opencode (title "Claude, Cursor and opencode Token Usage") and header toggles per provider (`ccu.providers`). Data comes from opencode's SQLite file, read-only, v2 layout only. Cost is often US$0.00 and there is no plan data. Docs updated: README, CONFIGURATION, DATA-CONTRACT, ARCHITECTURE, INSTALL, TROUBLESHOOTING, DEPLOYMENT. Code was not touched. Known gap: totals are about 0.4 percent off opencode's own, cause unexplained.

## Gotchas
- Claude on macOS keeps OAuth in the Keychain ("Claude Code-credentials"), no `.credentials.json`.
- Claude token refresh writes back to `.credentials.json` with default permissions and can race Claude Code. Make it read-only or chmod 0600.
- `allow_reuse_address=False` (`server.py:63-64`) must be True on POSIX.
- CRLF shebang in `hooks/claude_subagent_hook.py` breaks direct execution on POSIX.
- The page hard-codes its refresh interval, ignoring `token_refresh_s`/`plan_refresh_s` in usage.json.
- No JS tests exist. `tests/parity_check.py:27-28` has hard-coded absolute paths.
- Session slip: a `cd` into the source project in one Bash call moved the session working directory there. Use absolute paths and do not cd into the source folder.
