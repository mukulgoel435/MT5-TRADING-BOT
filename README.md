# MT5 XAUUSD Scalping Bot

Automated gold scalper for MetaTrader 5. Two live strategies, backtested across **9 months of real M5 data** (Oct 2025 – Jun 2026).

> ⚠️ Demo account only. Not financial advice. Past results do not guarantee future performance.

---

## Strategies

### Strategy A — M5 Trend Following
Trades trend continuation on 5-minute gold candles during London and NY sessions.

**Entry filters (all must be true):**
- ADX ≥ 25, rising for 2 consecutive bars, and below 50 (acceleration, not exhaustion)
- MACD histogram in correct direction for 3 consecutive bars (sustained momentum)
- MACD size ≥ 2% of ATR (filters zero-line noise)
- RSI > 50 for buys / < 50 for sells
- M15 EMA5/20 agrees with M5 direction
- 18-factor confluence score above session threshold (20 / 22 / 24)

**Trade management:**
- Stop loss: last 5-bar swing low/high + buffer
- Take profit: 1.5× SL distance (1.5R)
- Exit early only if MACD reverses while trade is at a loss (profit-conditional)
- Breakeven lock at 1R, trailing stop at 1.5R

**Blocked sessions:** Asian (00:00–08:00 UTC) and pre-NY indecision (12:00–13:00 UTC)

---

### Strategy B — London Session Breakout
Trades the break of the Asian session's price range at London open. No confluence scoring needed — the range break is the setup.

**Logic:**
1. Calculate Asian session high/low (22:00–07:00 UTC)
2. Filter: range must be 8–35 points (thin days and volatile days excluded)
3. Entry: M5 close breaks above/below range + 0.25×ATR buffer at 07:00–10:00 UTC
4. H1 EMA20/50 must agree with breakout direction
5. Stop loss: opposite side of range; Take profit: 1.5R; Max 1 trade per day

---

## Backtest Results (9 months — Oct 2025 to Jun 2026)

| Metric | Value |
|---|---|
| Net return | **+271.3%** ($200 → $742) |
| Win rate | **46.9%** |
| Profit factor | **1.45** |
| Max drawdown | **20.1%** |
| Profitable months | **7 / 9 (78%)** |
| Avg trades/day | 1.9 |

| Strategy | Trades | Win Rate | Profit Factor | Net P&L |
|---|---|---|---|---|
| A — M5 Trend | 277 | 46% | 1.42 | +$500 |
| B — London Breakout | 9 | **67%** | **13.04** | +$43 |

**Best session:** NY continuation 16:00–18:00 UTC — **55% WR, +$388**  
**Strongest single factor:** Fibonacci zone — +10.6% more present in wins, +13.5% WR lift

**Bad months:** February (−7%) and May (−2%) 2026 — choppy FOMC-driven conditions. All other 7 months profitable.

---

```
mt5-trading-bot/
├── config.example.py      → copy to config.py, add your demo login
├── requirements.txt
├── src/
│   ├── strategy.py        → the trading logic (improve this)
│   ├── risk.py            → position sizing + daily loss limits
│   ├── backtest.py        → backtest engine
│   └── mt5_connector.py   → MT5 connection, data, orders, history
├── scripts/
│   ├── test_connection.py → RUN FIRST: verify MT5 link + live data
│   ├── run_backtest.py    → backtest on real history
│   ├── check_trades.py    → see open positions + closed P/L
│   └── run_live.py        → auto-trade the demo account
├── data/                  → CSV logs land here
└── .vscode/launch.json    → F5 run menu
```

## Setup (one time)

**1. Open the folder in VS Code** — File → Open Folder → `mt5-trading-bot`.
Install the Microsoft **Python** extension if prompted.

**2. (Recommended) create a virtual environment**
In the VS Code terminal:
```
python -m venv .venv
.venv\Scripts\activate        # Windows
```

**3. Install dependencies**
```
pip install -r requirements.txt
```

**4. Add your demo credentials**
```
copy config.example.py config.py     # Windows
```
Open `config.py` and fill in `LOGIN`, `PASSWORD`, `SERVER` (the three values
from MT5's "Open an Account" dialog), plus `SYMBOL` and `TIMEFRAME`.
> `config.py` is git-ignored, so your password is never committed.

**5. In MT5 itself:** keep the terminal open, logged into the demo, and click
the **Algo Trading** button so it's green. The Python API can't trade otherwise.

## Run order

Use the **Run and Debug** panel (▶ icon) → pick from the dropdown → press F5.
Or run in the terminal:

| Step | Command | What it does |
|------|---------|--------------|
| 1 | `python scripts/test_connection.py` | Confirms the link, prints account + live candles |
| 2 | `python scripts/run_backtest.py` | Backtests on real history, saves trades |
| 3 | `python scripts/check_trades.py` | Shows open positions + closed P/L |
| 4 | `python scripts/run_live.py` | Auto-trades the demo (Ctrl+C to stop) |

**Always do step 1 first.** If it prints `Connected | DEMO | ...`, you're wired
up. The #1 snag is a wrong `SERVER` name — copy it exactly from MT5.

## The improvement loop
Backtest → demo-test for weeks → measure over many trades → change ONE thing in
`strategy.py` → re-backtest → compare. Avoid overfitting (don't tweak until the
past looks perfect) and remember demo fills are cleaner than live.
