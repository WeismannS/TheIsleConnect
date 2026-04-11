# The Isle Auto-Connect

Automated server reconnection bot for The Isle.

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set Windows screen scaling to 100%.

4. Run:
   ```bash
   python main.py
   ```

## How it works

The bot monitors your screen for UI elements and automatically:
- Navigates to the Unofficial servers tab
- Refreshes until your target server appears
- Connects to the server
- Dismisses connection errors and reconnects

This is essentially a smart macro
## Configuration

Edit `main.py` to change:
- `confidence` - Template matching sensitivity (0.0-1.0)
- `grayscale` - Toggle color mode for matching
- Target server by updating `assets/targets/target.png`

## Target Selection

Template matching is not perfectly accurate and may select a wrong server. For reliable targeting:
1. Favorite only your target server
2. Set screen scaling to 100%
3. Keep the game window in a consistent position
