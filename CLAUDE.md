# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A join / rejoin bot for The Isle (Evrima), plus auto safe-log on low hunger/thirst. Everything lives in one file, `isle_reconnect.py`: Win32 via `ctypes`, Windows' built-in OCR via `winrt`, and a `customtkinter` window. It is **Windows-only** and drives the real game, so nothing beyond `py_compile` runs on Linux.

## Developing from WSL

The repo sits in WSL, but the code must run under the Windows Python (`python.exe`, 3.14, on PATH via interop):

- **Syntax check (works anywhere):** `python3 -m py_compile isle_reconnect.py`.
- **Running Windows-side code:** copy `isle_reconnect.py` into a folder under `/mnt/c/...` and run `python.exe` there. Python run against a `\\wsl$` path is unreliable.
- **Config file:** the script reads and writes `isle_reconnect.json` next to itself, so a scratch copy creates its own. Delete it afterwards.
- **Pausing in shell loops:** `sleep` inside loops can be blocked by the harness. Use `timeout N tail -f /dev/null`, or do the waiting in a Python script on the Windows side.

Commands (`python isle_reconnect.py <cmd>`; no command opens the GUI):

- `run [--server NAME] [--debug] [--exit-on-join] [--rejoin-after-leave]`: the bot in the console.
- `scan`: OCR the game window once.
- `stats [--exact]`: hunger/thirst from the icons. `--exact` adds the game's tooltip values (focuses the game and opens chat) for checking the calibration.
- `status`: game state from the log.
- `stop`: signal a running bot.

**Build the exe** (from a Windows venv that has `requirements.txt` + `pyinstaller` installed):

```
pyinstaller --onefile --collect-data customtkinter --exclude-module PIL --exclude-module numpy isle_reconnect.py
```

Releases are GitHub releases (`gh release create vX.Y.Z isle_reconnect.exe`) with the exe attached; README links to `releases/latest`.

There is no test suite. Logic has been tested by monkeypatching `read_stats` / `read_exact` and swapping fake `Game`, `GameLog`, `Navigator` and `Ocr` objects onto a `Bot` instance, then calling `Bot._step()` / `_watch_stats()` directly.

## Config file in git

`isle_reconnect.json` is committed as the **blank default**: `DEFAULT_CONFIG` serialized with `json.dumps(..., indent=2)`. The local copy holds the user's real settings and is marked `git update-index --skip-worktree`.

To change the shipped default:
1. Regenerate it from `DEFAULT_CONFIG`, e.g. via `ast.literal_eval` piped into `git hash-object -w --stdin`.
2. Stage it with `git update-index --cacheinfo`.
3. Re-apply `--skip-worktree` after committing.

Never commit the user's local file.

`load_config` merges dict-valued sections (`hotkeys`, `safe_log`) key by key over the defaults. `labels` lists are merged with the user's entries first.

## Architecture

**Game state comes from the game's log, not the screen.**
- `LogTail` follows `%LOCALAPPDATA%\TheIsle\Saved\Logs\TheIsle.log`.
- `GameLog` turns the `RE_*` markers into the states `starting` / `menu` / `joining` / `loading` / `ingame` / `closed` and emits events (`joining`, `join_failed`, `disconnected`, `left`, `menu`, `ingame`, `closed`).
- The markers come from real Evrima client logs. Treat them as observed facts, not guesses.

**The screen is only used to act, and to check the log when it seems stuck.**
- `Navigator.tick()` does one OCR pass and the single most useful click: start prompt → session-filter dropdown → refresh → find the server row → Connect → dismiss popups.
- Buttons are found by text (`cfg["labels"]`, fuzzy matched by `find_label`). There are no image templates.

**`Bot`** is the loop. `run()` holds a single-instance mutex and the hotkeys, and `_loop()` wraps each `_step()` in a broad `except` with backoff, so one bad pass never ends the bot.

`_step()`:
- polls log events (`_on_event`)
- relaunches the game through Steam if it isn't running
- kills a game that has been "Not Responding" for 5 min
- while `ingame`: runs `_check_ingame` (HUD gone 2 min + menu on screen → treated as a disconnect) and `_watch_stats` (safe-log)
- times out `joining` / `loading` by checking the screen (`_where`)
- in `menu` / `starting`: ticks the Navigator and escalates when nothing is recognised (Esc at 45 s, game restart at 3 min)

**Game settings (`GameIni`, the More settings dialog's Game tab)** edit the game's `GameUserSettings.ini` / `Engine.ini` line by line, never through a parser: Unreal inis repeat keys (`ActionMappings`). The files are UTF-8 without a BOM, with CRLF line endings. Only changed values are written, and the first write keeps a `.bak`. **The game rewrites `Engine.ini` while it runs, keeping only its own lines.** This was observed: on joining a server it rewrote the file to update `CachedClientID`, and our `[SystemSettings]` and `[/Script/Engine.Engine]` lines were gone.
- It does read them at startup: after a save, changes were visible in game. Which keys took effect hasn't been confirmed one by one.
- The app's config is therefore the record: `game_engine` holds the Engine.ini keys we own (`engine_keys()`), and `game_pending` holds GameUserSettings changes saved while the game was open.
- `sync_game_files()` writes both, only while the game is closed. It runs on app start, a few seconds after the game exits (`App._watch_game`), after Save, and in `Bot._step` before a relaunch.
- Don't make Engine.ini read-only instead: the game needs to update `CachedClientID` between connections.

Found by disassembling the exe:
- `BloomQuality` / `LensFlareQuality` / `MotionBlurQuality` in `[/Script/Engine.RendererSettings]` run the matching `r.*` command.
- `[CustomVariables]` only holds mouse sensitivity.
- On every map load and every settings apply, the game resets `r.Nanite`, `r.ShadowQuality`, `r.Water.SingleLayer`, `grass.*` and `foliage.DensityScale`. Don't offer those.

**Adaptive brightness (`AutoBrightness`)** is a thread owned by `App`.
- It measures the middle 60% of the game's client area: a 48×27 HALFTONE `StretchBlt` from the screen DC.
- It maps that brightness onto the display gamma ramp with `SetDeviceGammaRamp`.
- The ramp is applied at scan-out, so screen and `PrintWindow` captures (the bot's OCR and HUD reads) never see it.
- It is only active while the game is in the foreground.
- Your original ramp is backed up to `isle_reconnect_gamma.bin`, which is restored on the next start after a kill.

The GUI (`App`) runs `Bot.run()` on a thread and reads `bot.events` / `bot.stats` every 100 ms. `log()` is redirected to the GUI through `log_sink`.

**Hunger / thirst (`read_stats`)** measures the stomach and water-drop icons at the bottom right.
- `STAT_ICONS` holds the 0–100% fill band of each icon, as pixels in from the bottom-right corner at 1080p, scaled by `h / 1080`.
- The bands were fitted to the game's own tooltip values, not to the drawn icon. The stomach's 0% line is below its drawn bottom.
- Fill pixels are told from the glowing outline by whiteness (`g - r < 40`). The outline can be just as bright.
- A solid red icon means the stat is empty (0%) and has no tooltip.
- The estimate is within about 1 point at 1080p. Below ~5% the fill is invisible and reads 0.

**The bot never needs focus while in game.** `_watch_stats` safe-logs on the icon estimate alone (three low readings in a row, or at once on a red icon), and `_clear_overlays` closes the pause menu and chat with posted keys. Keep it that way: the user tabs out while it runs.

**Exact values (`read_exact`)** come from the game's tooltip and are only used by `stats --exact`, for calibration. The sequence is fragile:
1. Bring the game to the front and press Enter to open chat, which frees the cursor.
2. Move the real cursor over the icon with `SendInput`.
3. OCR the tooltip, which appears up and to the right of the cursor, from a **screen** capture.
4. Close chat with Enter.

OCR has misread 55.42 as 5.47, so never act on one tooltip read without cross-checking the estimate.

**Safe-log (`_safe_log`):**
- Clears overlays first (`_clear_overlays`), then taps and holds the rest key via `Game.hold_key`.
- `hold_key` / `post_key` **post** `WM_KEYDOWN`/`WM_KEYUP` to the game window, so they work with the game in the background. Posted Ctrl isn't seen as held, so chat is emptied with repeated Backspace, not Ctrl+A.
- Once a safe-log is in progress, any later `left` / `disconnected` / `menu` event pauses the bot instead of rejoining.
- After a safe-log, a stat only triggers again once it has been seen above its limit.

## Hard-won game / Windows facts

Verified on the live game. Don't "fix" code that relies on these:

- **Esc opens the pause menu even while chat is open.** Close chat with **Enter** (empty message = nothing sent) and never with Esc. Enter toggles chat.
- **Chat open = white typing bar** at the chat box's bottom-left (`chat_open`). The chat box doesn't scale in proportion to the window, so only sample its left part.
- **Tooltips are separate OS windows.** `PrintWindow` captures (`Game.capture`, the default) miss them, so `read_exact` sets `use_screen = True`.
- **Posted keys work in the background:** the H rest/sleep hold, Esc for the pause menu, Enter/Backspace for chat.
- **Posted mouse messages do nothing:** no hover tooltip, no menu clicks, and arrow keys don't navigate the pause menu. Unreal uses the real cursor position, so reading exact stats always needs the game in front.
- **`Ocr.read` returns coordinates in client pixels** whatever the capture scale.
- **The window size changes:** the user has switched between 1920×1080 and 1600×900. Anything positional must scale with `h / 1080` and be checked at both sizes.
- **Safe-log in game:** rest (tap H), then hold H to sleep. The server logs you out after 60 s asleep, and moving cancels it.

Testing against the live game changes the user's session: it opens chat or the pause menu, and makes the dino rest or sleep. Ask before running anything that sends input, and leave chat and the menu closed afterwards. `Ocr` + `find_any(lines, ["RESUME"])` and `chat_open(game.capture())` check the menu and chat.
