# Strategy H — Institutional Session Flow (ISF)
### Full Documentation: GOLD + BTCUSD | XM Global Demo | Last updated: 2026-07-03

---

## 1. Overview

Strategy H trades **Institutional Session Flow** on the M15 timeframe. The core idea: large
institutions create liquidity sweeps, break structure, and then retest those levels. We wait for
a confirmed structural move, identify the retest, and enter in the direction of the dominant flow
with tight ATR-normalized risk.

The same strategy logic runs on two instruments simultaneously:

| Instrument | XM Symbol | Sessions        | Pip Size | Pip Value |
|------------|-----------|-----------------|----------|-----------|
| Gold       | GOLD      | Weekdays 24h    | 0.1      | $10 / lot |
| Bitcoin    | BTCUSD    | 24/7 (all days) | 1.0      | $1 / lot  |

Both instruments share the same entry/exit engine. GOLD carries six extra tuning parameters
to compensate for its choppier, trendier character vs. BTC.

---

## 2. Timeframe Stack

```
D1   →  Market Regime (Bull / Bear / Range / Chop / Compress)
H4   →  Directional Bias (Bull / Bear / None)
M15  →  Entry patterns (Pattern A / B / C)
```

Every M15 bar is only evaluated if the higher timeframes agree. Chop and Compress regimes block
all trading entirely.

---

## 3. Market Regime Filter (D1)

The D1 regime is computed once per day from the last 14 daily bars.

| Regime      | Condition                                              | Action          |
|-------------|--------------------------------------------------------|-----------------|
| **BULL**    | EMA5 > EMA20, body/ATR > 0.5, upper wick < 0.4×body  | Longs only (GOLD) / both (BTC) |
| **BEAR**    | EMA5 < EMA20, body/ATR > 0.5, lower wick < 0.4×body  | Shorts only (GOLD) / both (BTC) |
| **RANGE**   | EMA5 ≈ EMA20 (within 0.3×ATR), mid wick range         | Both directions |
| **CHOP**    | Overlapping candles, indecision                       | **No trading**  |
| **COMPRESS**| ATR < 0.5 × 20-day avg ATR                            | **No trading**  |

GOLD uses a **regime direction filter** (`regime_dir_filter = True`):
- D1 BULL → only take LONG entries
- D1 BEAR → only take SHORT entries
- D1 RANGE → both directions allowed

BTC does **not** use the direction filter (both directions trade in all valid regimes).

---

## 4. H4 Direction Bias

From the last 8 H4 bars:

- **Bull**: H4 EMA5 > EMA20 and last close above midpoint
- **Bear**: H4 EMA5 < EMA20 and last close below midpoint
- **None**: mixed / ambiguous

H4 direction adjusts the `daily_bias` signal which informs scanner logging and optional
filtering (but does not hard-block trades by itself — regime filter does that).

---

## 5. Daily Bias

Computed once per day from recent M15 candles and overlapping with the regime:

| Bias         | Meaning                                     |
|--------------|---------------------------------------------|
| `long`       | Strong long flow — regime + H4 aligned long |
| `long_pref`  | Mild long preference                        |
| `short`      | Strong short flow                           |
| `short_pref` | Mild short preference                       |
| `neutral`    | No clear preference                         |

Bias is informational in dashboard; hard directional blocking is done by the regime filter.

---

## 6. Session Windows

| Instrument | UTC Hours     | Weekends |
|------------|---------------|----------|
| GOLD       | 00:00 – 24:00 | Off (weekdays only) |
| BTCUSD     | 00:00 – 24:00 | **On** (all 7 days)  |

No intra-day session restriction (not limited to Asian/London/NY) — full 24h scanning within the
allowed calendar days.

---

## 7. Entry Patterns (M15)

All patterns require:
- ATR > `atr_low_factor × 20-bar ATR avg` (GOLD: 0.5×, BTC: 0.4×) — filters low-volatility bars
- Spread ≤ max allowed (GOLD: 3.0 pips, BTC: 50 pts)
- No open position on that instrument
- D1 not in Chop or Compress
- Daily loss limit not breached
- Consecutive loss halt not active

### 7.1 Pattern A — KZ Sweep + Fair Value Gap

```
1. Price sweeps below a prior swing low (liquidity grab)
2. A Fair Value Gap (FVG) forms immediately after the sweep
3. Entry: limit order at the midpoint of the FVG
4. SL: below the sweep extreme
```

Used exclusively for GOLD (and even there rarely triggers — Pattern B dominates).
Pattern A uses a **pending limit order** in MT5 and stays active until next bar.

### 7.2 Pattern B — Break of Structure Retest  *(primary)*

```
1. Price makes a clear Break of Structure (BOS):
   - Long setup:  bullish BOS = body closes above prior swing high by ≥ 0.45 × ATR
   - Short setup: bearish BOS = body closes below prior swing low by ≥ 0.45 × ATR
2. Price retraces into the BOS level
3. Entry confirmation: entry candle's wick touches the BOS zone
4. Entry: market order at current price
5. SL: below/above the BOS extreme + ATR buffer
```

This is the **primary pattern** for both instruments (~83% of GOLD trades, ~83% of BTC trades).

ATR thresholds (Pattern B):
- Min BOS body: `0.45 × ATR`
- Max retrace distance: `2.0 × ATR`
- Min entry wick: `20% of bar range`
- Sweep extreme buffer: `0.8 × ATR` from the swing point

### 7.3 Pattern C — Equal Level Sweep

```
1. Two or more swing highs/lows cluster within 0.3 × ATR (equal levels = EQH / EQL)
2. Price sweeps beyond the cluster by ≥ 0.15 × ATR
3. Entry: market order after the sweep candle closes back inside the cluster
4. SL: beyond the sweep extreme
```

BTC trades Pattern C. GOLD does **not** (`pattern_b_only = True` for GOLD — backtesting showed
Pattern C added noise on gold without meaningful edge).

---

## 8. Entry Mechanics

### Risk Sizing
```
lot = (account_balance × risk_pct) / (sl_distance_in_pips × pip_value)
```

- **Risk per trade**: 1.0% of current balance
- Minimum lot: 0.01
- Lot rounded to 2 decimal places

### Spread Filter
Bars with spread above the threshold are skipped entirely:
- GOLD: max spread = 3.0 pips
- BTC:  max spread = 50 points

### ATR Low Filter
Bars where ATR is below the historical average are skipped (avoids entering into dead,
compressed price action):
- GOLD: `ATR < 0.5 × 20-bar avg ATR` → skip
- BTC:  `ATR < 0.4 × 20-bar avg ATR` → skip

---

## 9. Exit Mechanics

### 9.1 TP1 Partial Close
When price reaches **TP1** (configurable Risk:Reward from entry):
- Close **50% of the position** (half-lot)
- Move SL to **breakeven** (entry price)
- Activate trailing SL for the remaining half

TP1 distance from entry:
- **GOLD**: 1.5 × SL distance (TP1_RR = 1.5)
- **BTC**: 1.0 × SL distance (TP1_RR = 1.0)

### 9.2 Trailing Stop Loss
After TP1 hit, the SL trails in the direction of the trade:
- Trail step: `0.5 × ATR` per move
- Each time price moves `0.5 × ATR` in our favor, SL advances by `0.5 × ATR`
- Counter `trail_moves` increments for analysis

### 9.3 Time Stop
Positions still open after **48 bars** (12 hours on M15) are closed at market:
- Exit reason: `TIME_STOP`
- Prevents stale trades from holding overnight unnecessarily

### 9.4 Regime Exit
If D1 regime shifts to **Chop** or **Compress** while a position is open:
- Position is closed at market immediately
- Exit reason: `REGIME_CHANGE`

---

## 10. Risk Management

### 10.1 Daily Loss Limit
If today's realized P&L drops below a percentage threshold, no new entries are taken for
the rest of the day:
- **GOLD**: 1.5% of balance (`daily_loss_limit = 0.015`)
- **BTC**: 2.0% of balance (default)

### 10.2 Consecutive Loss Halt
| Losses in a row | Action                |
|-----------------|-----------------------|
| ≥ 3             | Risk reduced (warning) |
| ≥ 6             | All new entries halted |

After halt cooldown (next day / session), consecutive loss counter behavior:
- **GOLD**: reset to 0 (`halt_reset_zero = True`) — prevents infinite halt cycling
- **BTC**: reduce to 3 (default CONSEC_REDUCE)

### 10.3 ATR Low Guard
Prevents entries during volatility collapse — see Section 8.

---

## 11. GOLD-Specific Tuning

GOLD required additional tuning vs. BTC because it is:
- More prone to whipsaws (large spreads, news-driven)
- Stronger trending in D1 (better to filter out counter-trend entries)
- Less liquid on certain regimes (Pattern C adds noise)

| Parameter         | GOLD Value | BTC Value | Why GOLD differs                          |
|-------------------|------------|-----------|-------------------------------------------|
| `tp1_rr`          | 1.5        | 1.0       | Wider TP needed due to large avg-loss     |
| `pattern_b_only`  | True       | False     | Pattern C noisy on gold                   |
| `regime_dir_filter`| True      | False     | Trend-following improves PF in trending gold |
| `halt_reset_zero` | True       | False     | Prevents halt-cycle on volatile sessions  |
| `daily_loss_limit`| 1.5%       | 2.0%      | Tighter daily stop due to large SL sizes  |
| `atr_low_factor`  | 0.5        | 0.4       | Higher volatility floor for gold          |

---

## 12. Live Configuration (XM Global Demo)

```
Account : 318314013
Server  : XMGlobal-MT5 7
Terminal: C:\Program Files\XM Global MT5\terminal64.exe
```

| Field              | GOLD           | BTCUSD         |
|--------------------|----------------|----------------|
| XM Symbol          | GOLD           | BTCUSD         |
| M15 pip size       | 0.1            | 1.0            |
| Pip value          | $10 / lot      | $1 / lot       |
| Slippage tolerance | 0.05 pips      | 0.50 pts       |
| Magic number       | 20001          | 20002          |
| Default lot        | 0.01 (min)     | 0.01 (min)     |
| Risk per trade     | 1.0%           | 1.0%           |
| Sessions           | Weekdays 24h   | All days 24h   |

---

## 13. Backtest Results

### 13.1 BTCUSD — Backtest Summary

> Period: **Aug 2023 – Jul 2026** (~3 years) | Data: XM M15 bars

| Metric              | Value                     |
|---------------------|---------------------------|
| Starting Balance    | $1,000.00                 |
| **Final Balance**   | **$6,425.90 (+542.6%)**   |
| Net P&L             | +$5,425.90                |
| **Max Drawdown**    | **2.7%**                  |
| Total Trades        | 3,353                     |
| Win Rate            | 45.7% (1,533W / 1,820L)   |
| **Profit Factor**   | **1.86**                  |
| Avg Win             | $7.68                     |
| Avg Loss            | $3.48                     |
| Win / Loss Ratio    | 2.21×                     |
| TP1 Hit Rate        | 41.2% (1,383 trades)      |
| Profitable Months   | **34 / 37 (91.9%)**       |
| Gross Win           | $11,767.58                |
| Gross Loss          | $6,341.68                 |
| Exit: SL Hit        | 3,025 (90.2%)             |
| Exit: Time Stop     | 324 (9.7%)                |
| Exit: Trail         | 3 (0.1%)                  |

**By Direction:**

| Direction | Trades | Win Rate | Net P&L    |
|-----------|--------|----------|------------|
| Long      | 2,111  | 45.5%    | +$2,986.24 |
| Short     | 1,242  | 46.1%    | +$2,439.66 |

**By Pattern:**

| Pattern   | Trades | Win Rate | Net P&L    |
|-----------|--------|----------|------------|
| B (BOS)   | 2,775  | 45.3%    | +$4,480.26 |
| C (EQL)   | 577    | 47.5%    | +$940.28   |

**By D1 Regime:**

| Regime | Trades | Win Rate | Net P&L    |
|--------|--------|----------|------------|
| Bull   | 2,204  | 45.9%    | +$3,252.50 |
| Bear   | 1,087  | 46.1%    | +$2,170.86 |
| Range  | 60     | 33.3%    | +$6.29     |

**Monthly Breakdown:**

| Month   | Trades | WR%   | P&L        | Balance    |
|---------|--------|-------|------------|------------|
| 2023-08 | 22     | 31.8% | −$6.01     | $993.99    |
| 2023-09 | 84     | 28.6% | −$13.27    | $980.72    |
| 2023-10 | 44     | 36.4% | +$21.50    | $1,002.22  |
| 2023-11 | 123    | 41.5% | +$62.66    | $1,064.88  |
| 2023-12 | 95     | 42.1% | +$69.18    | $1,134.07  |
| 2024-01 | 102    | 41.2% | +$69.78    | $1,203.85  |
| 2024-02 | 120    | 45.8% | +$152.44   | $1,356.29  |
| 2024-03 | 100    | 44.0% | +$225.55   | $1,581.84  |
| 2024-04 | 53     | 52.8% | +$118.46   | $1,700.30  |
| 2024-05 | 66     | 51.5% | +$92.33    | $1,792.63  |
| 2024-06 | 83     | 32.5% | +$42.00    | $1,834.63  |
| 2024-07 | 62     | 51.6% | +$92.44    | $1,927.06  |
| 2024-08 | 84     | 52.4% | +$188.86   | $2,115.92  |
| 2024-09 | 128    | 43.8% | +$97.95    | $2,213.87  |
| 2024-10 | 85     | 41.2% | +$119.52   | $2,333.39  |
| 2024-11 | 172    | 47.1% | +$425.28   | $2,758.67  |
| 2024-12 | 78     | 50.0% | +$136.72   | $2,895.39  |
| 2025-01 | 71     | 57.7% | +$202.78   | $3,098.17  |
| 2025-02 | 95     | 48.4% | +$316.80   | $3,414.98  |
| 2025-03 | 97     | 49.5% | +$150.44   | $3,565.42  |
| 2025-04 | 100    | 54.0% | +$332.77   | $3,898.18  |
| 2025-05 | 110    | 48.2% | +$245.02   | $4,143.20  |
| 2025-06 | 85     | 45.9% | +$151.83   | $4,295.03  |
| 2025-07 | 121    | 39.7% | +$120.24   | $4,415.28  |
| 2025-08 | 131    | 48.1% | +$175.47   | $4,590.75  |
| 2025-09 | 102    | 50.0% | +$170.75   | $4,761.50  |
| 2025-10 | 32     | 46.9% | +$174.97   | $4,936.47  |
| 2025-11 | 128    | 51.6% | +$453.29   | $5,389.76  |
| 2025-12 | 90     | 43.3% | +$141.98   | $5,531.74  |
| 2026-01 | 157    | 45.9% | +$174.14   | $5,705.88  |
| 2026-02 | 75     | 46.7% | +$185.93   | $5,891.82  |
| 2026-03 | 107    | 42.1% | +$87.58    | $5,979.40  |
| 2026-04 | 103    | 47.6% | +$94.16    | $6,073.56  |
| 2026-05 | 132    | 45.5% | +$76.86    | $6,150.41  |
| 2026-06 | 103    | 47.6% | +$263.87   | $6,414.28  |
| 2026-07 | 13     | 38.5% | +$11.62    | $6,425.90  |

---

### 13.2 GOLD — Backtest Summary

> Period: **Oct 2022 – Jul 2026** (~3.75 years) | Data: XM M15 bars | All 6 tuning params applied

| Metric              | Value                     |
|---------------------|---------------------------|
| Starting Balance    | $1,000.00                 |
| **Final Balance**   | **$3,581.80 (+258.2%)**   |
| Net P&L             | +$2,581.80                |
| **Max Drawdown**    | **61.7%**                 |
| Total Trades        | 2,096                     |
| Win Rate            | 41.0% (860W / 1,236L)     |
| **Profit Factor**   | **1.21**                  |
| Avg Win             | $17.11                    |
| Avg Loss            | $9.82                     |
| Win / Loss Ratio    | 1.74×                     |
| TP1 Hit Rate        | 32.2% (675 trades)        |
| Profitable Months   | **20 / 36 (55.6%)**       |
| Gross Win           | $14,714.66                |
| Gross Loss          | $12,132.86                |
| Exit: SL Hit        | 1,816 (86.6%)             |
| Exit: Time Stop     | 279 (13.3%)               |
| Exit: Trail/Regime  | 0 (0.0%)                  |

**By Direction:**

| Direction | Trades | Win Rate | Net P&L    |
|-----------|--------|----------|------------|
| Long      | 1,608  | 41.2%    | +$1,566.19 |
| Short     | 488    | 40.4%    | +$1,015.62 |

**By Pattern:**

| Pattern   | Trades | Win Rate | Net P&L    |
|-----------|--------|----------|------------|
| B (BOS)   | 2,095  | 41.1%    | +$2,605.06 |
| A (KZ)    | 1      | 0.0%     | −$23.26    |

*Pattern C disabled for GOLD (`pattern_b_only = True`).*

**By D1 Regime:**

| Regime | Trades | Win Rate | Net P&L    |
|--------|--------|----------|------------|
| Bull   | 1,597  | 41.0%    | +$1,340.17 |
| Bear   | 484    | 40.5%    | +$1,005.32 |
| Range  | 15     | 60.0%    | +$236.31   |

*Chop and Compress regimes produced zero trades — correct behavior.*

**Monthly Breakdown:**

| Month   | Trades | WR%   | P&L        | Balance    |
|---------|--------|-------|------------|------------|
| 2022-10 | 4      | 75.0% | +$11.73    | $1,011.73  |
| 2022-11 | 63     | 34.9% | −$129.05   | $882.68    |
| 2022-12 | 58     | 36.2% | −$92.17    | $790.51    |
| 2023-01 | 59     | 33.9% | −$73.19    | $717.32    |
| 2023-02 | 54     | 31.5% | −$63.47    | $653.85    |
| 2023-03 | 62     | 37.1% | +$33.67    | $687.52    |
| 2023-04 | 72     | 34.7% | −$58.98    | $628.53    |
| 2023-05 | 67     | 28.4% | −$40.80    | $587.73    |
| 2023-06 | 56     | 32.1% | −$53.98    | $533.75    |
| 2023-07 | 57     | 29.8% | −$50.47    | $483.28    |
| 2023-08 | 70     | 38.6% | −$51.05    | $432.23    |
| 2023-09 | 50     | 40.0% | −$26.20    | $406.03    |
| 2023-10 | 56     | 39.3% | +$74.13    | $480.15    |
| 2023-11 | 69     | 44.9% | +$17.40    | $497.55    |
| 2023-12 | 69     | 40.6% | +$4.21     | $501.76    |
| 2024-01 | 81     | 45.7% | +$79.64    | $581.39    |
| 2024-02 | 73     | 46.6% | +$0.74     | $582.13    |
| 2024-03 | 39     | 66.7% | +$114.23   | $696.36    |
| 2024-04 | —      | —     | $0.00      | $668.75*   |
| 2024-05 | —      | —     | $0.00      | D1 CHOP    |
| 2024-06 | —      | —     | $0.00      | D1 CHOP    |
| 2024-07 | —      | —     | $0.00      | D1 CHOP    |
| 2024-08 | —      | —     | $0.00      | D1 CHOP    |
| 2024-09 | —      | —     | $0.00      | D1 CHOP    |
| 2024-10 | 7      | 14.3% | −$27.61    | $668.75    |
| 2024-11 | 67     | 43.3% | +$112.69   | $781.44    |
| 2024-12 | 62     | 35.5% | −$73.77    | $707.67    |
| 2025-01 | 73     | 43.8% | +$59.45    | $767.13    |
| 2025-02 | 61     | 41.0% | +$34.48    | $801.61    |
| 2025-03 | 54     | 50.0% | +$180.45   | $982.06    |
| 2025-04 | 69     | 43.5% | +$345.20   | $1,327.26  |
| 2025-05 | 68     | 36.8% | +$109.49   | $1,436.75  |
| 2025-06 | 51     | 37.3% | −$10.16    | $1,426.59  |
| 2025-07 | 72     | 38.9% | −$114.14   | $1,312.45  |
| 2025-08 | 56     | 39.3% | −$97.07    | $1,215.38  |
| 2025-09 | 71     | 50.7% | +$273.34   | $1,488.71  |
| 2025-10 | 84     | 50.0% | +$640.03   | $2,128.74  |
| 2025-11 | —      | —     | $0.00      | D1 CHOP    |
| 2025-12 | —      | —     | $0.00      | D1 CHOP    |
| 2026-01 | —      | —     | $0.00      | D1 CHOP    |
| 2026-02 | —      | —     | $0.00      | D1 CHOP    |
| 2026-03 | 29     | 37.9% | +$204.79   | $2,333.53  |
| 2026-04 | 61     | 41.0% | +$91.76    | $2,425.29  |
| 2026-05 | 63     | 44.4% | +$209.77   | $2,635.05  |
| 2026-06 | 81     | 56.8% | +$1,093.61 | $3,728.66  |
| 2026-07 | 8      | 25.0% | −$146.86   | $3,581.80  |

*Blank months = D1 regime was CHOP or COMPRESS — no trades taken, balance unchanged.*

---

## 14. Comparison: GOLD vs. BTCUSD

| Metric              | BTCUSD              | GOLD                |
|---------------------|---------------------|---------------------|
| Return (total)      | **+542.6%**         | +258.2%             |
| Profit Factor       | **1.86**            | 1.21                |
| Max Drawdown        | **2.7%**            | 61.7%               |
| Win Rate            | **45.7%**           | 41.0%               |
| Avg Win             | $7.68               | **$17.11**          |
| Avg Loss            | **$3.48**           | $9.82               |
| Win/Loss $ ratio    | **2.21×**           | 1.74×               |
| TP1 hit rate        | **41.2%**           | 32.2%               |
| Profitable months   | **34/37 (91.9%)**   | 20/36 (55.6%)       |
| Dead months         | None                | ~10 (D1 Chop)       |
| Patterns            | B + C               | B only              |
| Direction filter    | None                | Regime-aligned only |
| Active schedule     | 7 days/week         | Weekdays only       |

**Why BTC outperforms GOLD on every risk metric:**
1. BTC has tighter pip economics ($1/lot vs $10/lot) → SL sizes are smaller in dollar terms relative to avg win
2. BTC trends more cleanly (less fundamental news shock whipsaws)
3. BTC's 24/7 schedule means more trade opportunities and compounding speed
4. GOLD's D1 Chop phases (mid-2024, late 2025) created 10+ dead months with no income
5. BTC Win/Loss ratio 2.21× vs GOLD 1.74× — BTC pays out more when it wins relative to what it loses

---

## 15. Files

| File                               | Purpose                              |
|------------------------------------|--------------------------------------|
| `scripts/run_live_h_xm.py`         | Live bot (GOLD + BTC)                |
| `scripts/backtest_h_xm.py`         | Backtest engine                      |
| `scripts/web_dashboard.py`         | Dashboard server (port 5050)         |
| `src/strategy_h/patterns.py`       | Pattern A/B/C detection logic        |
| `src/strategy_h/regime.py`         | D1/H4 regime + daily bias            |
| `src/strategy_h/exits.py`          | TP1 partial close + trail SL         |
| `src/strategy_h/sizing.py`         | Position sizing                      |
| `src/mt5_connector.py`             | MT5 API wrapper                      |
| `data/h_xm_live_status.json`       | Live bot state (both instruments)    |
| `data/h_xm_GOLD_trades.csv`        | GOLD live trade log                  |
| `data/h_xm_BTC_trades.csv`         | BTC live trade log                   |
| `data/logs/h_xm_YYYY-MM-DD.log`   | Daily bot log (UTC timestamps)       |
| `data/backtest_h_xm_BTCUSD_trades.csv` | BTC full backtest trade log      |
| `data/backtest_h_xm_GOLD_trades.csv`   | GOLD full backtest trade log     |

---

## 16. Known Limitations & Notes

- **GOLD drawdown (61.7%)**: Driven by the 2022–2023 losing streak before the strategy's edge compounded. Starting balance of $1,000 was drawn down to $406 (−60%) before recovering to $3,581. BTC only ever drew down 2.7% because its pip economics produce smaller % swings per trade.
- **GOLD dead months**: D1 CHOP and COMPRESS correctly blocked all trading during non-trending periods. These are not losses — they're zero-trade months. The flip side is BTC keeps compounding during those same periods.
- **Backtest overfitting risk**: The 6 GOLD tuning parameters were derived from the same data the strategy was backtested on. Forward performance may differ. BTC was not tuned (default parameters) and still produced superior results.
- **Instant SL recovery**: If a BTC position is entered and SL is hit before the Python process can read the open position (fast price moves), the bot now recovers the trade from MT5 deal history and writes it to CSV. See `INSTANT-CLOSE` in logs.
- **Data period**: BTC backtest starts Aug 2023 (XM historical data limit). GOLD backtest starts Oct 2022.
