# The Isle Auto-Connect

Automated server reconnection bot for The Isle.

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
1. Take a screenshot of your server's name in-game
2. Save it as `assets/targets/bosch.png` (or any name)
3. Open `main.py` and change `TARGET = "bosch.png"` to match your filename

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

This is essentially a smart macro.

## Target Selection

Template matching is not perfectly accurate and may select a wrong server. For reliable targeting:
1. Favorite only your target server
2. Set screen scaling to 100%
3. Keep the game window in a consistent position
