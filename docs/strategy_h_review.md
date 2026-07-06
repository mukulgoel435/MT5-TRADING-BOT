# Strategy H — Pre-Live Parameter Review
### Quantitative Research Notes | 2026-07-03

Scope: parameter and rule changes only. Architecture (D1 → H4 → M15 ISF) is kept exactly as-is.
Goal: higher Profit Factor, lower drawdown, better live consistency — not maximum backtest return.

---

## Key Findings Before Recommendations

The exit-reason breakdown reveals two non-obvious facts that overturn some intuitive assumptions:

| Exit Type | GOLD (N) | GOLD WR | GOLD Net | BTC (N) | BTC WR | BTC Net |
|-----------|----------|---------|----------|---------|--------|---------|
| SL Hit    | 1,816    | 37.2%   | +$1,745  | 3,025   | 45.6%  | +$5,459 |
| Time Stop | 279      | **65.9%**| +$826   | 324     | 46.3%  | −$63    |

**Finding A — GOLD time stop is producing meaningful alpha.**
65.9% WR on time-stop exits means trades that drift past 48 bars without hitting SL or TP1 are disproportionately profitable. They account for 32% of GOLD's total net P&L ($826 of $2,581) from only 13.3% of trades. These are slow-burn institutional setups where the BOS retest drifted quietly in direction without a sharp swing. The time stop is the only mechanism catching their profit. Do not shorten it.

**Finding B — GOLD TP1 at 1.5× is overshooting the typical swing distance.**
32.2% TP1 hit rate vs 65.9% time-stop WR tells a consistent story: price is not typically running 1.5× SL distance before reversing — it's running partway and stalling. Many wins are being captured at time stop at small profit ($2.96 avg) rather than at TP1 at larger profit ($17.11 avg). Lowering TP1_RR would convert these drifting winners into proper TP1 closes and raise WR on the position's first half.

**Finding C — GOLD is currently dead in live trading.**
Every M15 bar is skipped because live XM GOLD spread = 5.4–5.9 pips but the filter threshold is 3.0 pips. The backtest used a spread cost simulation (not a live-spread gate), so the backtest results were achieved without this filter. This is the single most critical defect.

**Finding D — BTC time stop is essentially neutral.**
46.3% WR, −$63 net across 324 trades over 3 years. Not meaningful enough to warrant changing in either direction.

---

## Recommendations

### 🔴 URGENT — Fix before any live session

**R1. GOLD: Raise spread filter from 3.0 to 5.5 pips**
- Applies to: **GOLD only**
- Reason: Live XM GOLD spread is consistently 5.4–5.9 pips. The current 3.0 threshold blocks 100% of entries. The backtest did not use a live-spread gate — it applied a fixed spread cost to the fill price. The live filter is a post-hoc addition that is miscalibrated vs. actual XM conditions.
- Implementation: Set `max_spread_pips = 5.5` for GOLD. On rare news events spreads widen to 8–12 pips; 5.5 will correctly reject those without rejecting normal trading hours.
- Expected impact: Restores normal GOLD trade frequency to approximately backtest levels. Without this, the strategy is inactive on GOLD regardless of all other settings.

---

### 🟠 HIGH PRIORITY — Implement before forward test

**R2. GOLD: Lower TP1_RR from 1.5 to 1.2**
- Applies to: **GOLD only**
- Reason: GOLD time-stop exits (65.9% WR, avg +$2.96) are the symptom. They are trades that moved in direction but never reached TP1 at 1.5× SL. Reducing to 1.2× means those same setups would hit TP1 more often — each producing a first-half close at +1.2R followed by a breakeven trail on the remainder. TP1 hit rate should increase from ~32% to ~38–42% (estimate). The avg win per TP1 trade is lower (1.2× vs 1.5×) but TP1 hit frequency increases, and many trades that previously exited at time stop for small profit are now properly recorded as TP1 hits.
- Not 1.0× because GOLD's larger candles (ATR ~7 pips live) need slightly more room than BTC to avoid TP1 being hit on noise.
- Expected impact: Modest PF improvement, meaningfully higher TP1 hit rate, better psychological consistency for forward testing analysis.
- Note: Do not change BTC TP1_RR (1.0× is correctly calibrated for BTC's sharper swing character).

**R3. GOLD: Reduce risk-per-trade from 1.0% to 0.75%**
- Applies to: **GOLD only**
- Reason: GOLD's 61.7% MaxDD is driven by the 2022–2023 sustained losing period (balance $1,000 → $406 before recovery). This is not a fluke — it reflects a genuine period of strategy underperformance during macro uncertainty. While the strategy eventually recovered, a 60%+ DD is unacceptable for a live account. At 0.75% risk, the same period would have produced approximately 46% MaxDD. The return path stays positive; compound growth is slower but live survivability is significantly better. BTC MaxDD is only 2.7% so BTC risk does not need reduction.
- Expected impact: GOLD MaxDD reduces from ~62% to ~46%. Net return over the backtest period reduces from +258% to approximately +180–190%. PF is unchanged (risk sizing does not affect PF).

**R4. BTC: Add breakeven buffer after TP1 — set SL to entry − (0.25 × ATR) instead of exactly entry**
- Applies to: **BTC only**
- Reason: After TP1 is hit, the current code moves SL exactly to entry price. On BTC, a single volatile M15 candle can briefly dip 0.1–0.3× ATR below entry before continuing higher. In live trading, the SL triggers at the exact entry price — which backtest price data, using close prices, doesn't fully replicate. A buffer of 0.25× ATR below entry absorbs this noise and keeps the trade alive through the brief pullback. GOLD does not need this (smaller pip economics, less intra-bar volatility as a % of SL size).
- Expected impact: Small increase in remaining-half trade duration, marginally better net P&L on trades that would otherwise prematurely stop at breakeven. Most important benefit is live-backtest consistency (reducing phantom slippage effect).

---

### 🟡 MODERATE PRIORITY — Implement if rebacktest confirms, otherwise monitor in forward test

**R5. Both: Tighten BOS maximum retrace distance from 2.0× to 1.5× ATR**
- Applies to: **Both**
- Reason: A BOS retest that has pulled back 2.0× ATR is back near the original BOS trigger candle's body. At that depth, the structural argument for the setup has significantly weakened — price is not holding above the break, it's fallen back into the prior range. Strong institutional follow-through retests at 1.0–1.5× ATR. Trades entered at 1.5–2.0× ATR retrace are lower quality and more likely to represent failed BOS.
- Caveat: No per-retrace-depth WR data available without rebacktesting. The reduction in trade count (estimated 10–15%) may or may not improve PF enough to justify it. Strong market logic but uncertain magnitude.
- Expected impact: Reduced trade count, likely improved WR, uncertain net effect on PF. Forward test for 30 days before deciding whether to keep.

**R6. BTC: Raise spread filter from 50 to 65 pts**
- Applies to: **BTC only**
- Reason: Live BTC spread on XM is consistently at exactly 50.0 pts in log data. This means BTC entries are filtered by rounding and timing — the bot sees spread=50.0 and at the exact threshold, sometimes allowing entries and sometimes not depending on tick timing. A threshold of 65 pts gives a clean 30% buffer above normal spreads while still rejecting genuine spike events (100+ pts). Current threshold creates edge-case instability in entry frequency.
- Expected impact: Marginally more consistent entry behaviour. On news events (spreads 80–150 pts), still correctly filtered.

**R7. GOLD: Lower consecutive-loss halt from 6 to 5**
- Applies to: **GOLD only**
- Reason: With GOLD WR of 41%, the probability of 5 consecutive losses by chance is 0.59⁵ = 7.2%, vs 0.59⁶ = 4.2% for 6. Moving from 6 to 5 triggers the halt slightly earlier without making it meaningfully more frequent. Given GOLD's higher DD risk profile versus BTC, the earlier circuit breaker is justified. BTC at 45.7% WR has P(5 consecutive losses) = 0.54³⁵ = 5.2% so the same threshold is slightly more aggressive for BTC — leave BTC at 6.
- Expected impact: Marginal reduction in DD during extended losing runs. Small reduction in trade count.

---

### ⬛ DO NOT CHANGE

| Parameter | Current | Verdict | Reason |
|-----------|---------|---------|--------|
| GOLD time stop (48 bars) | 48 bars | Keep | 65.9% WR on time stops — this is the second-largest contributor to GOLD profits |
| BTC time stop (48 bars) | 48 bars | Keep | 46.3% WR, net neutral — no meaningful edge in either direction |
| BTC TP1_RR | 1.0× | Keep | Well-calibrated. BTC's TP1 hit rate of 41.2% and $7.68/$3.48 win/loss ratio are already strong |
| SL buffer (0.8× ATR) | 0.8× | Keep | Tested across 5,449 trades. The WR and PF are consistent across both instruments |
| GOLD pattern_b_only | True | Keep | Pattern A fired once in 3.75 years. Pattern C backtested as noisy for gold |
| GOLD regime_dir_filter | True | Keep | Correctly filters counter-trend entries in trending gold markets |
| BTC session (24/7) | 24/7 | Keep | 91.9% profitable months validates the schedule |
| GOLD session (weekdays) | Weekdays | Keep | Working as intended; chop months had zero trades which is correct |
| ATR BOS body threshold (0.45×) | 0.45× | Keep | Not overfit — it is a structural minimum for BOS validity |
| GOLD daily_loss_limit (1.5%) | 1.5% | Keep | Backtest-validated. Changing without rebacktest introduces untested behavior |
| BTC risk per trade (1.0%) | 1.0% | Keep | MaxDD only 2.7% — enormous headroom. No justification for reducing |
| Trail step (0.5× ATR) | 0.5× | Keep | Very few trail exits is by design — TP1 partial close is the primary exit mechanism, not trail running |
| BTC D1 Range entries | Allowed | Monitor | 33.3% WR vs 46% in trending, but only 60 trades across 3 years. Sample too small to conclude — re-evaluate after 6 months forward testing |

---

## Summary Table

| # | Change | Instrument | Priority | Risk of Overfitting |
|---|--------|-----------|----------|---------------------|
| R1 | Spread filter 3.0 → 5.5 pips | GOLD | 🔴 Urgent | None (corrects calibration error) |
| R2 | TP1_RR 1.5 → 1.2 | GOLD | 🟠 High | Low (supported by time-stop WR data) |
| R3 | Risk 1.0% → 0.75% | GOLD | 🟠 High | None (pure risk management) |
| R4 | BE buffer: entry − 0.25×ATR | BTC | 🟠 High | None (live-backtest consistency fix) |
| R5 | BOS retrace 2.0× → 1.5× ATR | Both | 🟡 Moderate | Low-moderate (no per-depth WR data) |
| R6 | BTC spread filter 50 → 65 pts | BTC | 🟡 Moderate | None |
| R7 | Halt threshold 6 → 5 | GOLD | 🟡 Moderate | None (risk management) |

Total changes: 7. None alter the core ISF pattern logic or timeframe stack.
Implement R1–R4 immediately. Run R5–R7 in the next backtest iteration before committing.

---

## Expected Forward Behaviour After R1–R4

- GOLD begins taking live entries (currently inactive due to R1)
- GOLD TP1 hit rate rises from ~32% toward ~38–42%
- GOLD MaxDD in adverse periods reduces from ~62% to ~46%
- BTC fewer premature breakeven stops after TP1 on intra-bar spikes
- Both instruments: overall PF expected to stay equal or improve slightly
- Trade count remains approximately the same (only R5 would reduce count)
