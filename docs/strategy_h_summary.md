# Strategy H — Quick Reference

**Name:** Institutional Session Flow (ISF)
**Instrument:** XAUUSD (Gold) | **Timeframe:** M15
**Account:** $1,000 Demo | MetaQuotes-Demo | Login: 5052559332
**Live since:** 2026-07-02

---

## What It Does

Strategy H trades gold by following what institutional participants (banks, hedge funds) do at session opens. Before trending, institutions sweep retail stop clusters — equal highs/lows, prior session extremes — to fill large orders cheaply. Strategy H waits for that sweep, confirms the reversal, and enters in the institutional direction.

**Three-layer regime filter (no entries unless all align):**
- D1 structure → is gold in a bull, bear, or range swing?
- H4 direction → which way is momentum pointing this session?
- Daily bias → should we prefer longs, shorts, or both?

**Three entry patterns:**
- **Pattern B** — BOS (Break of Structure) forms on M15 → price pulls back to that level → rejection candle → enter. *(Primary — 77% of all trades)*
- **Pattern C** — Equal highs or lows cluster on M15 → last bar sweeps and closes back above/below → enter at close. *(23% of trades)*
- **Pattern A** — Kill Zone + Asian range sweep + FVG confirmation → limit order at FVG midpoint. *(Rare — triple confluence)*

**Exit management:**
1. TP1 at 1:1 RR → close 50%, move SL to breakeven
2. Trail remaining 50% with ATR-based stop (1.5× ATR from best price seen)
3. Time stop at 20 bars (~5 hours) if TP1 not reached
4. Regime exit if D1+H4 flip fully against the trade (after TP1 only)

**Risk rules:**
- 1% risk per trade (0.5% after 3 consecutive losses)
- Halt after 6 consecutive losses → 24h cooldown → resume at 0.5%
- Daily loss limit: −2% of balance
- No CHOP or COMPRESS regime entries

---

## 2-Year Backtest Results (Jul 2024 – Jul 2026, XAUUSD M15)

| Metric | $200 account | $1,000 account |
|---|---|---|
| Final balance | $1,043 | **~$5,216** |
| Total return | **+421.6%** | **+421.6%** |
| Net P&L | +$843 | **+$4,216** |
| Monthly return (avg) | ~17.6%/mo | ~17.6%/mo |
| Total trades | 331 (24 months) | same |
| Win rate | **50.8%** | 50.8% |
| Profit factor | **1.49** | 1.49 |
| Max drawdown | 22.9% (~$46) | 22.9% (~$229) |
| Profitable months | **18 / 24 (75%)** | 18 / 24 |
| Avg win / avg loss | $15.30 / $10.59 | ~$76 / ~$53 |
| Pattern B (254 trades) | WR 51.6%, PF 1.59 | — |
| Pattern C (77 trades) | WR 48.1%, PF 1.18 | — |
| Long trades (305) | PF 1.45 | — |
| Short trades (26) | PF 2.04 | — |

---

## Files

| File | Purpose |
|---|---|
| `scripts/run_live_h.py` | Live trading bot — runs 24/7 |
| `scripts/web_dashboard.py` | Dashboard at http://localhost:5050 |
| `src/strategy_h/regime.py` | D1/H4 regime + daily bias |
| `src/strategy_h/patterns.py` | Pattern A/B/C detection |
| `src/strategy_h/exits.py` | TP1, trailing SL, time stop |
| `src/strategy_h/sizing.py` | Lot sizing (1% risk) |
| `data/h_trades.csv` | All closed live trades |
| `data/h_live_status.json` | Real-time bot state |
| `data/logs/h_YYYY-MM-DD.log` | Detailed per-bar decision log |
| `docs/strategy_h_design.md` | Full design document (18 sections) |
