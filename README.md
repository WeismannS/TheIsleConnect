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

## Setup

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
python isle_reconnect.py run --server "part of the server name"
```

The server name is saved to `isle_reconnect.json`, so after the first run `python isle_reconnect.py run` is enough.

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
| `run [--server NAME]` | Join and keep rejoining automatically |
| `run --exit-on-join` | Stop once you're in |
| `run --rejoin-after-leave` | Rejoin even when you leave or safe-log yourself (saved) |
| `run --debug` | Print what OCR reads on each pass |
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

## Tips

- Run `python isle_reconnect.py scan` if the bot can't find something. It shows exactly what OCR sees.
- Matching ignores case and tolerates small OCR errors, so a distinctive part of the server name is enough.
