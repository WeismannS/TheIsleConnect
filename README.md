# The Isle Auto-Connect

Automatically retry and reconnect to your favorite The Isle server. No more manual refreshing.

> The Isle removed its queue system. This bot monitors your screen and handles the retry loop for you.

## Requirements

- Windows or Linux
- Python 3.x ([Microsoft Store](https://apps.microsoft.com/store/search/python) on Windows)
- The Isle running in **fullscreen**

## Setup

**1. Create and activate a virtual environment**

```cmd
python -m venv .venv
```

Windows:
```cmd
.venv\Scripts\activate.bat
```

Linux/macOS:
```bash
source .venv/bin/activate
```

**2. Install dependencies**

```cmd
pip install -r requirements.txt
```

**3. Configure your target server**

1. In-game, **favorite only** the server you want to join
2. Take a screenshot of the server name as it appears in-game
3. Save it to `assets/targets/` — e.g. `assets/targets/myserver.png`
4. Open `main.py` and set:
```python
   TARGET = "myserver.png"
```

**4. Run**

```cmd
python main.py
```

## How it works

Once running, the bot will:

1. Navigate to the **Favorite servers** tab
2. Refresh until your target server appears
3. Attempt to connect
4. Dismiss any connection errors and retry automatically

## Tips

- Only favorite your target server — other favorites can confuse the matcher
- Keep the game window in a consistent position between runs
- The template screenshot must match what's on screen at your current resolution/DPI — retake it if you change display settings