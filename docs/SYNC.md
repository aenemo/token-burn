# Using Token Burn on several computers

Token Burn can combine the usage from every computer you use into one dashboard. Each computer keeps collecting on its own and writes a small summary file into a folder that your own sync app (OneDrive, Dropbox, iCloud Drive, Google Drive) or a network share copies to your other computers. Each computer then reads the others' files and adds them to its own dashboard. There is no server, no account and no network listener: Token Burn only reads and writes files.

Sync is optional and off by default. With sync off, Token Burn behaves exactly as it did before.

## The three folders

Token Burn deals with three different folders. Keep them apart.

| Folder | What it holds | Synced? |
|---|---|---|
| Program folder | The Token Burn app (exe, `_internal`, launcher). Per-OS default: Windows `%LOCALAPPDATA%\Programs\Token Burn`, macOS `~/Applications/Token Burn`, Linux `~/.local/share/token-burn` | No. Never inside a synced folder |
| Data folder | The live SQLite database (`usage.db`), `usage.json`, logs, `config.json`, the peer cache. Per-OS default: Windows `%LOCALAPPDATA%\token-burn\data`, macOS `~/Library/Application Support/token-burn/data`, Linux `~/.local/state/token-burn` | No. Never synced, never on a network share |
| Shared folder | A `token-burn` folder with one small summary file per computer. Lives inside a folder your sync app copies, for example `OneDrive\token-burn` | Yes. This is the one that must be synced |

Why the data folder must not be synced: it holds a live SQLite database that is written every minute. Cloud sync apps copy files at arbitrary moments and can corrupt a database, create conflict copies, or lock it while uploading. The installer refuses synced and network folders for the data folder and for the program folder. The shared folder is the opposite: it holds only small, atomically written files designed to be synced.

## What is shared, and what is not

The shared folder looks like this:

```
<your synced folder>/token-burn/
  README.txt                      a short note saying what the folder is
  machines/<machine-id>.json.gz   one file per computer, written only by that computer
  control/<machine-id>.json       small overrides (rename, retire, remove), written by any computer
```

Each computer's file holds only: token counts per minute, per model and effort (input, output, cache read and write, requests), costs, subagent run summaries (type, model, effort, status, token counts, cost), plan usage percentages with their reset times, and this computer's display name, host name, operating system and Token Burn version.

It never holds: prompts, messages, code, conversation titles, session ids, file paths, credentials, tokens or anything from Claude, Cursor, opencode or OpenAI other than the usage numbers above. Subagent ids are hashed with the machine id before they are written.

Files from other computers are treated as untrusted data. They are size capped, type checked, clamped and never executed.

## First computer

1. Run the installer (see [INSTALL](INSTALL.md)).
2. At "Do you use Token Burn on more than one computer?" choose "Start a new shared folder (first computer)".
3. Pick a synced folder, for example your OneDrive folder. The installer lists the synced folders it found, or opens a folder window. Pick the folder itself, not a `token-burn` folder inside it (Token Burn creates that).
4. Give this computer a name, for example "Office PC".
5. Confirm. Token Burn creates `token-burn/machines`, `token-burn/control` and a README in the folder.

Command line equivalent:

```
token-burn install --sync-folder "C:\Users\me\OneDrive" --new --machine-name "Office PC"
```

Passing `--sync-folder` (or `--yes`) means no questions are asked. `--new`, `--join`, `--machine-name` and `--adopt` need `--sync-folder`, otherwise the command exits 2. If the folder does not look synced a note is printed and the install carries on. See the flag table in [INSTALL](INSTALL.md).

## Joining from another computer

1. Wait until the first computer's `token-burn` folder has synced to this computer.
2. Run the installer and choose "Join an existing shared folder".
3. Pick the same folder you picked on the first computer (the one that contains `token-burn`).
4. The installer lists the computers already using the folder. Choose a name for this one. Names must be unique.

```
token-burn install --sync-folder "/Users/me/Library/Mobile Documents/com~apple~CloudDocs" --join --machine-name "MacBook"
```

If you pick a folder that already holds Token Burn files while starting a new shared folder, the installer offers to join instead. If you choose "Join" and the folder has no Token Burn files, it says so and asks whether to start a new shared folder there instead, so you can check you picked the right folder. Without a terminal, `--join` into a folder with no Token Burn files exits 3. A name already used by another computer exits 3 too (in the wizard it asks for another name).

## Third and further computers

Join exactly as above. Each computer you add appears in the dashboard's computer toggle row and in the Computers table. Up to 64 computers are read per pass. Nothing needs changing on the existing computers.

## Replacing a computer

When you retire an old computer, its history should still count but it should stop showing a "stale" warning. On any computer:

```
token-burn sync machines                 # list computers and their state
token-burn sync retire "Old office PC"   # keep its history, stop the stale warning
token-burn sync unretire "Old office PC" # reverse it
token-burn sync remove "Old office PC"   # delete its file and its history from the shared folder
```

- **Retired**: its data still counts and it is still listed (marked Retired) and can be toggled on the dashboard. It no longer shows a stale warning.
- **Removed**: its file is deleted and a small tombstone is written. Peers ignore it. If that computer is somehow still running, it stops exporting and reports "This computer was removed from the shared folder. Run token-burn sync enable to rejoin." Rejoining deletes the tombstone and creates a new id.

`remove` asks you to type the computer's name to confirm unless you pass `--yes`. Without a terminal and without `--yes` it is refused with exit 3. Names are matched case-insensitively. If a name is ambiguous the command lists the candidates (exit 3), and you can use the id instead (8 to 32 hex characters, as shown by `sync machines`). A name or id that matches no computer exits 1. You cannot retire or remove the computer you are running on: use `token-burn sync disable --remove-my-data` for that.

You can also rename: `token-burn sync rename "New name"` renames this computer, and `token-burn sync rename "New name" --machine "Old name"` renames another one through a control file.

The wizard also offers to retire any computer that has not reported for more than 7 days when you join.

## Reinstalling and adopting an existing identity

Each computer has a random machine id stored in its data folder (`config.json`). It survives reinstalls and upgrades. It is lost if you delete the data folder, run `token-burn uninstall --purge-data`, or reinstall Windows.

When a computer joins and has no id, but the shared folder already holds an active computer with the same host name, the wizard asks "Is this computer 'Office PC' (for example, you reinstalled Windows or Token Burn)?" (the default is yes) If you answer yes, this computer adopts that identity: the existing history is copied to `adopted-<id>.json.gz` in the data folder and merged into the new exports, so the history stays under one name and is not lost. Non-interactively use `--adopt "Office PC"` with `--sync-folder` (it implies join and cannot be combined with `--new`). After adopting, the existing file is accepted once even when this computer has a different host name. If a computer's id is lost without adopting (the data folder was wiped), its old file stays in the shared folder; Token Burn recognises it as the same physical computer by `host_fp`, counts the two files once (per-cell maximum, not a sum) and shows the old one as "Superseded by <name>". Adopted history is also included in this computer's own dashboard. It exits 3 if no computer has that name in the shared folder, or if this computer already has an id (run `token-burn sync reset-id` first).

## Cloned VMs and copied data folders: reset-id

If you clone a virtual machine, or copy a data folder from one computer to another, two computers will share one machine id and write the same file. Token Burn notices this: it compares a random write token in the existing file with the last one this computer wrote. Each file also carries a privacy-safe hardware fingerprint (`host_fp`, a salted hash of the OS machine id; the raw id is never written). A file with the same fingerprint as this computer is this computer (for example after restoring a backup of the data folder), so it is never a conflict. When a computer with a different fingerprint is writing with the same id it stops exporting (merging continues) and shows "Another computer (hostname X) is writing with this computer's ID. Run token-burn sync reset-id on one of them."

On one of the two, run:

```
token-burn sync reset-id
```

This gives the computer a new id and clears its export state, then collects once. It asks for confirmation; without a terminal and without `--yes` it is refused with exit 3. The old file is left in the shared folder for you to retire or remove.

## How the numbers are combined

| Platform | Rule |
|---|---|
| Claude, opencode | Added. Each computer's minute totals are summed. The dashboard's computer toggles filter by computer |
| Cursor | Not added. Cursor usage comes from your Cursor account, so it is the same on every computer. Per minute and model, the larger value wins, so nothing is double counted. Cursor is not filtered by computer |
| Plan usage (Claude, Cursor) | The newest sample from any computer is used for the current figures, and the history is the union of all computers' samples. If your computers use different accounts the lines would interleave, which is not supported |
| Subagent runs | Combined, with duplicates dropped by id. For Cursor the same conversation seen on two computers can appear twice |

Retired computers still count. Removed ones do not.

## When files are written

- Each computer writes its own file at most every 5 minutes, and only when its data has changed.
- An idle computer that is still running rewrites its file every 30 minutes (a heartbeat) so the others know it is alive.
- A computer is marked stale when it has not written for more than 1 hour. A computer that is switched off therefore shows as stale until it is turned on again, retired or removed.
- Peers' files are re-read every minute, so a change on one computer appears on another after your sync app has copied the file plus up to a minute or so.

Run `token-burn sync now` to write and read immediately.

## Work OneDrive

A work or school OneDrive (for example "OneDrive - Contoso") may be covered by your employer's IT policy, which can limit what you store there or monitor it. Token Burn only stores token counts, costs, model names and the computer's name and operating system, and never prompts, code or passwords. The installer shows this notice and asks you to confirm. If you are unsure, check with your IT team, or use a personal synced folder or a network share instead.

On Windows OneDrive, the installer also asks OneDrive to keep the `token-burn` files on the computer (always keep on this device) so they are never online-only placeholders. On macOS iCloud Drive, set "Keep Downloaded" on the `token-burn` folder.

## Status and settings

```
token-burn sync status        # on or off, folder, this computer's name and id, status, last export
token-burn sync enable        # turn sync on for an installed computer
token-burn sync disable       # turn sync off here; the shared folder is left alone
token-burn sync disable --remove-my-data   # also delete this computer's file from the shared folder
```

`sync enable` asks the same questions as the installer when run in a terminal. Without a terminal (or with `--yes`) it needs `--folder PATH` (exit 2 without it) and accepts `--new`, `--join` and `--machine-name`; with neither `--new` nor `--join` it joins when the folder already holds Token Burn files and otherwise starts a new one. It collects once when it finishes. `sync disable --remove-my-data` asks for confirmation (refused with exit 3 without a terminal and without `--yes`).

Exit codes for every `token-burn sync` command: 0 ok, 1 error (including a name or id that matches no computer, or sync being off), 2 bad arguments, 3 refused or cancelled. `sync remove`, `sync disable --remove-my-data` and `sync reset-id` ask for confirmation and are refused with exit 3 when there is no terminal and no `--yes`.

## Troubleshooting

See [TROUBLESHOOTING](TROUBLESHOOTING.md) for: shared folder not found, a stale computer, conflicting ids, files "not downloaded yet", work OneDrive limits and damaged files. In short:

- The dashboard has a Computers panel with each computer's last seen time and status, and a banner whenever sync is not "ok".
- `token-burn sync status` prints the same status in the terminal.
- A file that is still syncing or damaged shows the last good copy and a message, and recovers by itself.

## Manual test with two data folders on one PC

You can try the whole feature on a single computer, with two data folders standing in for two computers and an ordinary folder as the shared folder. Nothing is installed into the scheduler: `sync enable` only writes `config.json` in the data folder and files in the shared folder. Because `TOKEN_BURN_DATA_DIR` is set, the data folder is fixed and the usual checks for temporary folders do not apply. Expect the note "this folder does not look like it is synced to your other computers", because the shared folder is an ordinary folder.

Windows (PowerShell):

```
$env:TOKEN_BURN_DATA_DIR = "$env:TEMP\tb-A"
token-burn sync enable --folder "$env:TEMP\tb-shared" --new --machine-name PC-A --yes
token-burn sync now

$env:TOKEN_BURN_DATA_DIR = "$env:TEMP\tb-B"
token-burn sync enable --folder "$env:TEMP\tb-shared" --join --machine-name PC-B --yes
token-burn sync now
token-burn sync machines

$env:TOKEN_BURN_DATA_DIR = "$env:TEMP\tb-A"
token-burn sync now
token-burn sync machines
Remove-Item Env:TOKEN_BURN_DATA_DIR
```

macOS and Linux (bash):

```
export TOKEN_BURN_DATA_DIR=/tmp/tb-A
token-burn sync enable --folder /tmp/tb-shared --new --machine-name PC-A --yes
token-burn sync now

export TOKEN_BURN_DATA_DIR=/tmp/tb-B
token-burn sync enable --folder /tmp/tb-shared --join --machine-name PC-B --yes
token-burn sync now
token-burn sync machines

export TOKEN_BURN_DATA_DIR=/tmp/tb-A
token-burn sync now
token-burn sync machines
unset TOKEN_BURN_DATA_DIR
```

Each `sync enable` and `sync now` prints "Collected." The first `sync machines` (on B) lists PC-B as "this computer" and PC-A as "just now". The second (on A, after its second `sync now`) lists both, with PC-A as "this computer". Check that `tb-shared/token-burn/machines/` holds two `.json.gz` files, and that `usage.json` in each data folder has a `machines` array with two entries. To see the dashboard for one of them, run `token-burn serve --port 8431` with the same `TOKEN_BURN_DATA_DIR` set (do not use 8421, which the installed dashboard uses). Because both data folders are on the same physical computer, they share its hardware fingerprint: each side lists the other as "Superseded by" itself and merges the two by the larger value, so no totals are doubled. To see two entries added together you need two real computers. Delete the two data folders and `tb-shared` afterwards.
