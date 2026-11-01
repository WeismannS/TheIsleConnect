# The Isle Auto-Connect

Fully automated join / rejoin bot for The Isle (Evrima). Start it once and leave it running.

> The Isle removed its queue system. This bot handles the retry loop for you, and gets you back in after a disconnect.

## What it does

- **Reads the game's own log** (`%LOCALAPPDATA%\TheIsle\Saved\Logs\TheIsle.log`) to know exactly what's happening: joining, join failed (e.g. server full), in game, disconnected, closed.
- **Navigates the menus with Windows' built-in OCR.** It finds buttons by their text (Play, your server in the list, Connect, OK...), so you don't need template screenshots and resolution / window size don't matter.
- **Retries** as soon as a join fails, **rejoins** after a disconnect, and can **relaunch** the game through Steam if it crashes.

## Requirements

- Windows 10/11
- Python 3.9+
- The Isle running **windowed or borderless** (exclusive fullscreen captures as black)

## Quick start (no Python needed)

1. Download `isle_reconnect.exe` from the [latest release](https://github.com/WeismannS/TheIsleConnect/releases/latest) into its own folder
2. Double-click it, type part of the server's name, and press **Start**

Prefer the console? All the commands below work the same way with `isle_reconnect.exe` in place of `python isle_reconnect.py`. The config file is created next to the exe.

## Setup (from source)

**1. Create and activate a virtual environment**

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

**2. Install dependencies**

```cmd
pip install -r requirements.txt
```

(or `uv sync` if you use uv)

**3. Run**

```cmd
python isle_reconnect.py
```

opens the window. Or skip it and run the bot in the console:

```cmd
python isle_reconnect.py run --server "part of the server name"
```

The server name is saved to `isle_reconnect.json`, so after the first run `python isle_reconnect.py run` is enough.

## The window

A dark monitor built with [customtkinter](https://github.com/TomSchimansky/CustomTkinter): what the bot is doing and since when, attempt / join counters, a strip of every join attempt in the last 30 minutes (hover a tick for its time and result), and the activity log. Type part of the server's name, pick the session filter, and press **Start**. **Pause** / **Stop** do the same as the hotkeys, **Scan screen** prints what OCR sees (handy when the bot can't find something), and **Config file** (top right) opens `isle_reconnect.json` in Notepad. The settings are saved when you press Start.

**More settings** (in the Settings card) covers the rest of the config without editing the file: the hotkeys, the two timeouts, how much screenshots are enlarged for OCR, and extra button texts to look for if the game renames a button.

Its **Game** tab edits The Isle's own settings files in `%LOCALAPPDATA%\TheIsle\Saved\Config\WindowsClient`:

- **Quality**: the game's graphics levels (view distance, shadows, textures, effects, foliage, shading, landscape) and its frame rate limit.
- **Foliage**: the game's foliage physics switches (plants moving for you or for everyone, grass bending). All three cost CPU.
- **Fog**: fog detail (Game / Lower / Lowest) draws volumetric fog on a coarser grid. It stays just as thick but costs less. Written to `Engine.ini`, untested.
- **Visibility** (for private and sandbox servers; public servers ban for these): night brightness (display gamma), draw distance, and switches for height fog, volumetric fog and clouds. Written to `Engine.ini`, untested.
- **Adaptive brightness** (Off / Low / Medium / High): while the game is the active window, the app measures how dark the middle of the picture is about twice a second. In dark scenes it raises the screen's gamma, easing back as it gets lighter. Switching away from the game or turning it Off restores your normal gamma at once. It works live, even with the game open. It changes the display's gamma ramp, so it brightens everything on that monitor while active, and it overrides tools like f.lux or Night light while it runs. If the app is killed while it's boosting, the next start restores your gamma from `isle_reconnect_gamma.bin`.
- **Hidden effects**: bloom and lens flare. The game reads both from `GameUserSettings.ini`, but its menu doesn't show them.
- **Engine tweaks**: ambient occlusion, depth of field, chromatic aberration and film grain off, and sharpening, written to `Engine.ini` under `[SystemSettings]`. These haven't been tested on the current build, which may ignore them.

Close the game before saving: it rewrites these files when it exits, so the tab refuses to save while the game runs. Only the values you change are written. The first change keeps the original file next to it as `.bak`. Grass and foliage density aren't offered: the game resets them on every map load.

To build the exe yourself: `pip install pyinstaller` then `pyinstaller --onefile --collect-data customtkinter --exclude-module PIL --exclude-module numpy isle_reconnect.py` (the two excludes keep out optional libraries the app never uses, about 17 MB). Double-clicking it opens the window without a console; run from a terminal, it takes the commands below.

## Recovering on its own

Besides retrying joins and rejoining after disconnects, the bot gets itself out of these:

- **An unexpected error** is logged and the bot carries on after a short wait (2 s, doubling up to 60 s), so it never stops while you're away.
- **The game's log and the screen disagree.** If the log never says the menu loaded (60 s), or says you're in game while the HUD has been gone for 2 minutes, the bot checks the screen and goes by what it sees. An unlogged disconnect is treated as a disconnect.
- **An unrecognised screen** in the menus: after 45 s it presses Esc once (for a stray popup); after 3 minutes it closes the game so it's relaunched. A game that's still starting gets `launch_timeout` + 3 minutes.
- **A frozen game** (Windows says *Not Responding*) for 5 minutes is closed and relaunched.
- **Pause menu or chat in the way** while safe-log is on: the pause menu is closed (it blurs the HUD and swallows keys), and before a safe-log chat is cleared and closed, so H goes to the dino and not into chat. Both use keys posted to the game window, so they work in the background too.

## Auto safe-log

The bot can safe-log you when your dino gets hungry or thirsty. When hunger or thirst drops to the level you set, it rests (H), holds H to sleep, waits for the server to let you out (60 s), and then pauses instead of rejoining. The H presses go to the game window only, so they work while you're tabbed out and can't type into another app.

It reads the stomach and water-drop icons at the bottom right of the HUD every 5 s. The reading is calibrated against the game's own values and lands within about 1 point at 1920×1080. All of this works with the game in the background: the bot never brings it to the front, moves your mouse or opens chat while you play.

Set the levels in the window's Settings card (**Auto safe-log at**), or in `isle_reconnect.json`. While the bot runs, the card shows what it last read next to each level.

```json
"safe_log": {"hunger": 20, "thirst": 20, "key": "H", "hold_seconds": 2.0}
```

`0` turns a stat off (the default). Run `python isle_reconnect.py stats` in game to check what the bot reads.

- After a safe-log, a stat must be seen above its level again before it can trigger, so rejoining while still hungry doesn't log you straight back out.
- An empty stat (the icon turns solid red) reads as 0%.
- It waits for three low readings in a row (about 15 s), so one bad frame can't log you out. A solid red icon means the stat is empty and safe-logs at once.
- If you're still in game 90 s after a safe-log, it tries again, up to 3 times, then pauses.
- Keep the HUD visible. While the icons are hidden (map, menus) the bot just skips the check.

## Hotkeys

Global hotkeys (they work while the game has focus):

| Key | Action |
|-----|--------|
| F10 | Quit |
| F11 | Pause / resume |

Change them under `hotkeys` in `isle_reconnect.json` (any of F1-F24, PAUSE, SCROLLLOCK, INSERT, HOME, END). If another program already owns a key, the bot watches the key directly instead.

## Commands

| Command | What it does |
|---------|--------------|
| *(none)* or `gui` | Open the window |
| `run [--server NAME]` | Join and keep rejoining automatically |
| `run --exit-on-join` | Stop once you're in |
| `run --rejoin-after-leave` | Rejoin even when you leave or safe-log yourself (saved) |
| `run --debug` | Print what OCR reads on each pass |
| `stats [--exact]` | Read hunger and thirst once from the HUD. `--exact` also reads the game's tooltips to check the estimate (brings the game to the front and opens chat) |
| `stop` | Stop a running bot without the hotkey |
| `scan` | OCR the game window once and print the text it finds |
| `status` | Show what the log says the game is doing |

`--log PATH` (before the command) points at a different `TheIsle.log`.

## Configuration

`isle_reconnect.json` holds the settings:

| Key | Meaning |
|-----|---------|
| `server` | Part of the server's name as shown in the server list |
| `session_filter` | Server browser dropdown: `Official` / `Unofficial` / `Favourites` / `History` |
| `search_box` | Position of the "Server Name Filter" box as a fraction of the window (relearned automatically) |
| `ocr_scale` | How much to enlarge screenshots before OCR (the list font is small) |
| `labels` | Button texts to look for, in priority order. Edit these if the game renames a button |
| `hotkeys` | Keys for `quit` and `pause` |
| `join_timeout` | Seconds without a result before navigating again |
| `launch_timeout` | Seconds to wait for a relaunched game |
| `rejoin_after_leave` | Rejoin even when you leave the server yourself |
| `relaunch_after_quit` | Relaunch the game through Steam if it closes |
| `safe_log` | Hunger / thirst levels (%) that trigger a safe-log (`0` = off), the rest key, and how long to hold it |

## Tips

- Run `python isle_reconnect.py scan` if the bot can't find something. It shows exactly what OCR sees.
- Matching ignores case and tolerates small OCR errors, so a distinctive part of the server name is enough.
