"""
config.example.py
-----------------
Copy this file to `config.py` and fill in YOUR demo account details.

    Windows:  copy config.example.py config.py
    Mac/Lin:  cp config.example.py config.py

config.py is git-ignored so your password never gets committed.
"""

# ---- MT5 demo account (from the "Open an Account" dialog in MT5) ----
LOGIN    = 12345678               # your demo login number
PASSWORD = "your-demo-password"   # your demo password
SERVER   = "MetaQuotes-Demo"      # EXACT server name shown in MT5

# ---- What to trade ----
SYMBOL    = "EURUSD"   # "XAUUSD" for gold, "GBPUSD", "USDJPY", etc.
TIMEFRAME = "H1"       # M1, M5, M15, M30, H1, H4, D1

# ---- Risk settings ----
RISK_PCT        = 0.01    # risk 1% of balance per trade
MAX_DAILY_LOSS  = 0.05    # stop trading for the day after -5%
MAX_OPEN_TRADES = 1

# ---- Symbol economics (CHECK these with your broker!) ----
# XAUUSD (gold): pip_size 0.1,  pip value ~10/lot
# EURUSD/most FX: pip_size 0.0001, pip value ~10/lot
# JPY pairs: pip_size 0.01
PIP_SIZE          = 0.0001
PIP_VALUE_PER_LOT = 10.0
