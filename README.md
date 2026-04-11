# The Isle Auto-Connect

Automatically retry and reconnect to your favorite The Isle server when it opens up. No more sitting there hitting refresh.

The Isle removed the server queue system. Now you have to manually retry until you get in. This bot does that for you automatically.

## Setup

### 1. Install Python
Download Python from the Microsoft Store (search "Python 3.14") and install it.

### 2. Create a virtual environment
Open Command Prompt in this folder and run:
```cmd
python -m venv .venv
```

### 3. Activate the virtual environment
```cmd
.venv\Scripts\activate
```

### 4. Install dependencies
```cmd
pip install -r requirements.txt
```

### 5. Configure
Set Windows screen scaling to 100% in Display settings.

To change the target server:
1. Favorite only your target server
2. Take a screenshot of your server's name in-game
3. Save it as `assets/targets/bosch.png` (or any name)
4. Open `main.py` and change `TARGET = "bosch.png"` to match your filename

### 6. Run
```cmd
python main.py
```

## How it works

The bot monitors your screen for UI elements and automatically:
- Navigates to the Unofficial servers tab
- Refreshes until your target server appears
- Connects to the server
- Dismisses connection errors and reconnects

## Tips

For reliable targeting:
1. Favorite only your target server
2. Set screen scaling to 100%
3. Keep the game window in a consistent position
