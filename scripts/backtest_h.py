#!/usr/bin/env python3
"""
scripts/backtest_h.py — Strategy H Institutional Session Flow
2-Year M15 XAUUSD Backtest

Architecture:
  - Fetches 2yr M15 XAUUSD bars from MT5
  - Resamples to D1 and H4 for regime computation (NO look-ahead)
  - Updates daily context at 07:00 UTC (London Open) each day
  - Detects Pattern A/B/C setups with session timing and direction bias
  - Manages positions: limit fill, TP1 50% partial, swing trail, time stop
  - Full statistics: per-pattern, per-direction, monthly, exit-reason

Usage:
    python scripts/backtest_h.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math, csv
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import MetaTrader5 as mt5
import config

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_BULL, H4_BEAR, H4_NONE,
    BIAS_NEUTRAL,
)
from src.strategy_h.patterns import (
    detect_pattern_c, detect_pattern_a, detect_pattern_b, pattern_priority,
)
from src.strategy_h.exits import (
    compute_sl, compute_tp1,
    check_sl_hit, check_tp1_hit,
    check_time_stop, update_trail_sl,
    check_regime_exit, sl_distance_valid,
)
from src.strategy_h.sizing import compute_lot

# ── Simulation parameters ────────────────────────────────────────────────────
PIP_SIZE = 0.1
PIP_VAL  = 10.0
INIT_BAL = 200.0

RISK_PCT        = 0.01     # 1.0% per trade (standard)
RISK_PCT_REDUCED = 0.005   # 0.5% after CONSEC_REDUCE consecutive losses
SLIPPAGE        = 0.05     # pts added on SL fills and market orders
SPREAD_COST     = 0.15     # pts deducted as spread at entry (both sides)

SESSION_START = 6
SESSION_END   = 19

ATR_P  = 14
ATR_AP = 20
WARMUP = 250

DAILY_LOSS_LIMIT = 0.02    # halt day after -2% daily loss
CONSEC_REDUCE    = 3       # halve risk after 3 consecutive losses
CONSEC_HALT      = 6       # stop entirely after 6 consecutive losses

PATTERN_A_EXPIRE = 4       # bars a Pattern A limit order stays alive
PATTERN_BC_EXPIRE = 1      # B/C are market orders, expire after 1 bar

FLOAT_EPS = 1e-9


# ── ATR helpers ──────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, p: int = 14) -> float:
    """True Range ATR over last p bars of df."""
    if len(df) < 2:
        return 1.0
    h = df["high"].values[-p - 2:]
    l = df["low"].values[-p - 2:]
    c = df["close"].values[-p - 2:]
    n = len(h)
    if n < 2:
        return max(float(h[-1] - l[-1]), FLOAT_EPS)
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]),
                    np.abs(l[1:] - c[:-1])))
    use = tr[-min(p, len(tr)):]
    return float(use.mean()) if len(use) > 0 else 1.0


def _pnl(direction: str, entry: float, exit_p: float, lot: float) -> float:
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / PIP_SIZE * PIP_VAL * lot, 4)


def _pnl_r(direction: str, entry: float, sl: float, exit_p: float) -> float:
    sl_dist = abs(entry - sl)
    if sl_dist < FLOAT_EPS:
        return 0.0
    profit_pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(profit_pts / sl_dist, 3)


# ── Data fetch ───────────────────────────────────────────────────────────────

def fetch_m15(symbol: str = "XAUUSD", years: int = 2) -> pd.DataFrame:
    """Fetch M15 OHLCV bars from MT5 for the last `years` years."""
    if not mt5.initialize(login=config.LOGIN, password=config.PASSWORD,
                          server=config.SERVER):
        raise RuntimeError(f"MT5 initialisation failed: {mt5.last_error()}")

    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=years * 366 + 10)

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, start_dt, end_dt)
    if rates is None or len(rates) < 500:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 99999)

    mt5.shutdown()

    if rates is None or len(rates) == 0:
        raise RuntimeError("No M15 data returned from MT5.")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]

    # Trim to exactly `years` years from the latest bar
    cutoff = df.index[-1] - pd.Timedelta(days=years * 366)
    df = df[df.index >= cutoff].copy()

    print(f"  M15 bars loaded : {len(df):,}")
    print(f"  Period          : {df.index[0].date()}  →  {df.index[-1].date()}")
    return df


# ── Main simulation ──────────────────────────────────────────────────────────

def run_simulation(bars: pd.DataFrame):
    """
    Run the full Strategy H simulation on M15 bars.

    Returns: (closed_trades: list, final_balance: float, max_dd_pct: float)
    """
    n         = len(bars)
    balance   = INIT_BAL
    peak_bal  = INIT_BAL
    max_dd    = 0.0

    position  = None   # current open position dict
    pending   = None   # pending order dict (limit or market-on-next-open)

    closed       = []
    daily_pnl    = 0.0
    daily_date   = None
    consec_losses = 0
    halt_cooldown_bar = 0   # bar index when CONSEC_HALT cooldown expires

    # Daily context (updated at 07:00 UTC)
    d1_regime  = D1_RANGE
    h4_dir     = H4_NONE
    daily_bias = BIAS_NEUTRAL

    # Session-level state
    ab_h = -1e18;  ab_l = 1e18    # building Asian range (22:00–06:59 UTC)
    asian_h = None;  asian_l = None  # finalised prior Asian range
    pdh = None;  pdl = None;  pdc = None  # prior day high/low/close
    d1rh = None; d1rl = None             # D1 range for premium/discount
    last_ctx_date = None

    # Precompute D1 and H4 resamples from M15 data
    OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4_all = bars.resample("4h").agg(OHLCV_AGG).dropna()

    print(f"  D1 bars         : {len(d1_all)}")
    print(f"  H4 bars         : {len(h4_all)}")

    for i in range(WARMUP, n):
        bar = bars.iloc[i]
        t   = bar.name
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)

        hour   = t.hour
        date   = t.date()
        o_     = float(bar["open"])
        h_     = float(bar["high"])
        l_     = float(bar["low"])
        c_     = float(bar["close"])

        # ── ATR for this bar ─────────────────────────────────────────────────
        atr_window = bars.iloc[max(0, i - ATR_P - 5): i]
        atr        = _atr(atr_window, ATR_P)
        atr_avg    = _atr(atr_window, min(ATR_AP, len(atr_window)))

        # ── Reset daily loss counter ──────────────────────────────────────────
        if date != daily_date:
            daily_date = date
            daily_pnl  = 0.0

        # ── Build Asian session range (22:00–06:59 UTC) ───────────────────────
        if hour >= 22 or hour < 7:
            ab_h = max(ab_h, h_)
            ab_l = min(ab_l, l_)

        # ── Update daily context at 07:00 UTC (London Open) ──────────────────
        if hour == 7 and date != last_ctx_date:
            last_ctx_date = date

            # Finalise Asian range
            if ab_h > ab_l and ab_h != -1e18:
                asian_h = ab_h
                asian_l = ab_l
            ab_h = -1e18;  ab_l = 1e18

            # Prior day OHLCV from D1 resample
            d1_before = d1_all[d1_all.index.date < date]
            if len(d1_before) >= 2:
                pdh = float(d1_before.iloc[-1]["high"])
                pdl = float(d1_before.iloc[-1]["low"])
                pdc = float(d1_before.iloc[-1]["close"])
                d1_recent = d1_before.iloc[-min(20, len(d1_before)):]
                d1rh = float(d1_recent["high"].max())
                d1rl = float(d1_recent["low"].min())

            # D1 regime (strictly historical — no current day)
            if len(d1_before) >= 12:
                d1_regime = compute_d1_regime(d1_before)

            # H4 direction — use H4 bars whose CLOSE time <= 07:00 today
            h4_cutoff  = pd.Timestamp(year=date.year, month=date.month,
                                       day=date.day, hour=7, tzinfo=timezone.utc)
            h4_before  = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4_cutoff]
            if len(h4_before) >= 12:
                h4_dir = compute_h4_direction(h4_before)

            # Daily bias
            if d1rh is not None and d1rl is not None:
                daily_bias, _ = compute_daily_bias(
                    d1_regime, h4_dir, c_,
                    d1rh, d1rl, pdh, pdl, pdc,
                )

        # ════════════════════════════════════════════════════════════════════
        # MANAGE EXISTING POSITION
        # ════════════════════════════════════════════════════════════════════
        if position is not None:
            pos       = position
            direction = pos["direction"]
            bars_held = i - pos["open_bar"]

            sl_hit  = check_sl_hit(l_, h_, pos["current_sl"], direction)
            tp1_hit = (not pos["tp1_reached"] and
                       check_tp1_hit(l_, h_, pos["tp1"], direction))

            # SL hit — conservative: SL always wins over TP1 on same bar
            if sl_hit:
                fill = (pos["current_sl"] - SLIPPAGE if direction == "long"
                        else pos["current_sl"] + SLIPPAGE)
                remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
                pnl       = _pnl(direction, pos["entry_price"], fill, remaining)
                if pos["tp1_reached"]:
                    pnl += pos["tp1_pnl"]
                balance   += pnl
                daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                result     = "win" if pnl > 0 else "loss"
                consec_losses = 0 if result == "win" else consec_losses + 1

                closed.append({
                    **pos,
                    "exit_price":  fill,
                    "exit_reason": "SL_HIT",
                    "pnl":         pnl,
                    "pnl_r":       _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                    "bars_held":   bars_held,
                    "result":      result,
                    "balance_after": balance,
                })
                position = None
                pending  = None
                continue

            # TP1 hit — close 50%, move SL to breakeven
            if tp1_hit:
                half = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(direction, pos["entry_price"], pos["tp1"], half)
                pos["tp1_reached"] = True
                pos["tp1_pnl"]     = tp1_pnl
                pos["half_lot"]    = half
                pos["current_sl"]  = pos["entry_price"]   # SL to breakeven
                # Initialise trail from current swing
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                entry_in_window = max(0, 30 - bars_held)
                new_trail = update_trail_sl(
                    {"direction": direction,
                     "current_sl": pos["current_sl"],
                     "entry_bar": entry_in_window},
                    m15_w, atr,
                )
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail
                continue

            # After TP1: update swing trail every bar
            if pos["tp1_reached"]:
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                entry_in_window = max(0, 30 - bars_held)
                new_trail = update_trail_sl(
                    {"direction": direction,
                     "current_sl": pos["current_sl"],
                     "entry_bar": entry_in_window},
                    m15_w, atr,
                )
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail

                # Regime exit — full structural flip against position
                if check_regime_exit(pos, d1_regime, h4_dir):
                    fill  = c_ - SLIPPAGE if direction == "long" else c_ + SLIPPAGE
                    pnl   = (_pnl(direction, pos["entry_price"], fill, pos["half_lot"])
                             + pos["tp1_pnl"])
                    balance   += pnl
                    daily_pnl += pnl
                    peak_bal   = max(peak_bal, balance)
                    max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                    result     = "win" if pnl > 0 else "loss"
                    consec_losses = 0 if result == "win" else consec_losses + 1
                    closed.append({
                        **pos,
                        "exit_price":  fill,
                        "exit_reason": "REGIME_EXIT",
                        "pnl":         pnl,
                        "pnl_r":       _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                        "bars_held":   bars_held,
                        "result":      result,
                        "balance_after": balance,
                    })
                    position = None
                    continue

            # Time stop — close entire position if TP1 not reached in TIME_STOP bars
            if check_time_stop(bars_held, pos["tp1_reached"]):
                fill  = c_ - SLIPPAGE if direction == "long" else c_ + SLIPPAGE
                pnl   = _pnl(direction, pos["entry_price"], fill, pos["lot"])
                balance   += pnl
                daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                result     = "win" if pnl > 0 else "loss"
                consec_losses = 0 if result == "win" else consec_losses + 1
                closed.append({
                    **pos,
                    "exit_price":  fill,
                    "exit_reason": "TIME_STOP",
                    "pnl":         pnl,
                    "pnl_r":       _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                    "bars_held":   bars_held,
                    "result":      result,
                    "balance_after": balance,
                })
                position = None
                continue

        # ════════════════════════════════════════════════════════════════════
        # FILL PENDING LIMIT / MARKET ORDER
        # ════════════════════════════════════════════════════════════════════
        if pending is not None and position is None:
            p         = pending
            direction = p["direction"]

            if i > p["expires_bar"]:
                pending = None
            else:
                filled     = False
                fill_price = None

                if p["order_type"] == "market":
                    # Fill at current bar open (order placed on previous bar close)
                    filled     = True
                    fill_price = o_ + SLIPPAGE if direction == "long" else o_ - SLIPPAGE

                elif p["order_type"] == "limit":
                    ep = p["entry_price"]
                    if direction == "long" and l_ <= ep:
                        filled     = True
                        fill_price = ep + SLIPPAGE
                    elif direction == "short" and h_ >= ep:
                        filled     = True
                        fill_price = ep - SLIPPAGE

                if filled and fill_price is not None:
                    # Apply half-spread cost
                    fill_price += SPREAD_COST / 2 if direction == "long" else -SPREAD_COST / 2
                    fill_price  = round(fill_price, 5)

                    # Recompute SL/TP1 at actual fill price
                    sl  = compute_sl(fill_price, direction, p["atr"],
                                     p.get("sweep_extreme"))
                    tp1 = compute_tp1(fill_price, sl, direction)

                    if not sl_distance_valid(fill_price, sl, p["atr"]):
                        pending = None
                    else:
                        position = {
                            "direction":    direction,
                            "entry_price":  fill_price,
                            "sl":           sl,
                            "tp1":          tp1,
                            "current_sl":   sl,
                            "lot":          p["lot"],
                            "open_bar":     i,
                            "tp1_reached":  False,
                            "tp1_pnl":      0.0,
                            "half_lot":     max(0.01, round(p["lot"] * 0.5, 2)),
                            "pattern":      p["pattern"],
                            "atr_at_entry": p["atr"],
                            "entry_time":   str(t),
                            "d1_regime":    d1_regime,
                            "h4_dir":       h4_dir,
                            "daily_bias":   daily_bias,
                            "session":      p.get("session", "?"),
                            "balance_before": balance,
                            "sl_distance":  abs(fill_price - sl),
                        }
                        pending = None

        # ════════════════════════════════════════════════════════════════════
        # DETECT NEW PATTERNS
        # ════════════════════════════════════════════════════════════════════
        # Skip if already have a position or pending order
        if position is not None or pending is not None:
            continue

        # Session and risk filters
        if not (SESSION_START <= hour < SESSION_END):
            continue
        if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
            continue
        if consec_losses >= CONSEC_HALT:
            if halt_cooldown_bar == 0:
                halt_cooldown_bar = i + 96   # 24-hour cooldown (96 × 15 min)
            if i < halt_cooldown_bar:
                continue
            else:
                consec_losses     = CONSEC_REDUCE   # reset to reduced-risk level
                halt_cooldown_bar = 0
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue   # no Asian range yet — early warmup

        # Environment filter
        if atr_avg > 0:
            if atr > 3.0 * atr_avg:
                continue  # news spike
            if atr < 0.4 * atr_avg:
                continue  # dead market

        risk_pct   = RISK_PCT_REDUCED if consec_losses >= CONSEC_REDUCE else RISK_PCT
        m15_window = bars.iloc[max(0, i - 60): i + 1]

        found_setup = False
        for direction in ("long", "short"):
            if not bias_allows_direction(daily_bias, direction):
                continue

            setups = []

            # Pattern C — Equal H/L sweep (highest win rate, market entry)
            pc = detect_pattern_c(m15_window, direction, atr)
            if pc:
                setups.append(pc)

            # Pattern A — Kill Zone sweep + FVG (limit entry)
            pa = detect_pattern_a(
                m15_window, hour, direction,
                asian_h, asian_l, pdh, pdl, atr,
            )
            if pa:
                setups.append(pa)

            # Pattern B — BOS retest rejection (market entry)
            pb = detect_pattern_b(m15_window, direction, atr)
            if pb:
                setups.append(pb)

            setup = pattern_priority(setups)
            if setup is None:
                continue

            sweep_ext  = setup.get("sweep_extreme")
            test_entry = setup["entry_price"]
            sl_test    = compute_sl(test_entry, direction, atr, sweep_ext)

            if not sl_distance_valid(test_entry, sl_test, atr):
                continue

            lot = compute_lot(balance, abs(test_entry - sl_test), risk_pct)

            # Order type: B and C → market on next bar; A → limit
            if setup["type"] == "A":
                order_type   = "limit"
                expires_bar  = i + PATTERN_A_EXPIRE
            else:
                order_type   = "market"
                expires_bar  = i + PATTERN_BC_EXPIRE

            pending = {
                "direction":     direction,
                "entry_price":   test_entry,
                "sweep_extreme": sweep_ext,
                "lot":           lot,
                "atr":           atr,
                "pattern":       setup["type"],
                "order_type":    order_type,
                "expires_bar":   expires_bar,
                "session":       setup.get("session", "?"),
            }
            found_setup = True
            break   # one direction per bar

    # ── Force-close any open position at final bar ────────────────────────────
    if position is not None:
        pos       = position
        direction = pos["direction"]
        fill      = float(bars.iloc[-1]["close"])
        remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
        pnl       = _pnl(direction, pos["entry_price"], fill, remaining)
        if pos["tp1_reached"]:
            pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({
            **pos,
            "exit_price":  fill,
            "exit_reason": "FORCE_CLOSE",
            "pnl":         pnl,
            "pnl_r":       _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
            "bars_held":   n - 1 - pos["open_bar"],
            "result":      "win" if pnl > 0 else "loss",
            "balance_after": balance,
        })

    return closed, balance, max_dd


# ── Statistics output ────────────────────────────────────────────────────────

def print_stats(closed: list, final_balance: float, max_dd: float):
    SEP  = "─" * 70
    SEP2 = "═" * 70

    if not closed:
        print("  No trades generated.")
        return

    total  = len(closed)
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else 99.0
    net    = round(gw - gl, 2)
    ret    = round((final_balance - INIT_BAL) / INIT_BAL * 100, 1)
    wr     = round(len(wins) / total * 100, 1)
    avg_w  = round(gw / len(wins), 2)    if wins   else 0.0
    avg_l  = round(gl / len(losses), 2)  if losses else 0.0
    exp_r  = round(sum(t.get("pnl_r", 0) for t in closed) / total, 3)

    sl_hits    = sum(1 for t in closed if t.get("exit_reason") == "SL_HIT")
    time_stops = sum(1 for t in closed if t.get("exit_reason") == "TIME_STOP")
    trail_exits = sum(1 for t in closed if t.get("exit_reason") == "REGIME_EXIT")
    tp1_hits   = sum(1 for t in closed if t.get("tp1_reached"))

    # Per-pattern
    pat_map = defaultdict(list)
    for t in closed:
        pat_map[t.get("pattern", "?")].append(t)

    # Per-direction
    dir_map = defaultdict(list)
    for t in closed:
        dir_map[t.get("direction", "?")].append(t)

    # Per-regime
    reg_map = defaultdict(list)
    for t in closed:
        reg_map[t.get("d1_regime", "?")].append(t)

    # Monthly
    monthly = defaultdict(list)
    for t in closed:
        et = str(t.get("entry_time", ""))[:7]
        if et:
            monthly[et].append(t)

    print(f"\n{SEP2}")
    print(f"  STRATEGY H — INSTITUTIONAL SESSION FLOW")
    print(f"  2-Year M15 XAUUSD Backtest")
    print(f"{SEP2}")
    print(f"  Initial balance  : ${INIT_BAL:.2f}")
    print(f"  Final balance    : ${final_balance:.2f}  ({'+' if ret >= 0 else ''}{ret}%)")
    print(f"  Net P&L          : ${net:.2f}")
    print(f"  Max Drawdown     : {max_dd:.1f}%")
    print(f"{SEP}")
    print(f"  Total trades     : {total}")
    print(f"  Win Rate         : {wr}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor    : {pf}")
    print(f"  Avg Win          : ${avg_w:.2f}   Avg Loss: ${avg_l:.2f}")
    print(f"  Expectancy       : {exp_r:+.3f}R per trade")
    print(f"  Gross Win        : ${gw:.2f}   Gross Loss: ${gl:.2f}")
    print(f"{SEP}")
    print(f"  Exit reasons:")
    print(f"    SL Hit         : {sl_hits:>4}  ({round(sl_hits/total*100,1)}%)")
    print(f"    Time Stop      : {time_stops:>4}  ({round(time_stops/total*100,1)}%)")
    print(f"    Trail/Regime   : {trail_exits:>4}  ({round(trail_exits/total*100,1)}%)")
    print(f"  TP1 reached      : {tp1_hits:>4}  ({round(tp1_hits/total*100,1)}%)")

    print(f"\n{SEP}")
    print(f"  PER-PATTERN BREAKDOWN")
    print(f"  {'Pattern':<10} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*48}")
    for ptype in sorted(pat_map.keys()):
        pt = pat_map[ptype]
        pw  = [t for t in pt if t["pnl"] > 0]
        pl  = [t for t in pt if t["pnl"] <= 0]
        pg  = sum(t["pnl"] for t in pw)
        plo = abs(sum(t["pnl"] for t in pl))
        ppf = round(pg / plo, 2) if plo > 0 else 99.0
        pwr = round(len(pw) / len(pt) * 100, 1)
        pnl_s = f"${sum(t['pnl'] for t in pt):.2f}"
        print(f"  {'Pattern '+ptype:<10} {len(pt):>7} {pwr:>7} {ppf:>7} {pnl_s:>11}")

    print(f"\n{SEP}")
    print(f"  PER-DIRECTION BREAKDOWN")
    print(f"  {'Dir':<10} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*48}")
    for dr in ("long", "short"):
        dt = dir_map.get(dr, [])
        if not dt:
            continue
        dw  = [t for t in dt if t["pnl"] > 0]
        dl  = [t for t in dt if t["pnl"] <= 0]
        dg  = sum(t["pnl"] for t in dw)
        dlo = abs(sum(t["pnl"] for t in dl))
        dpf = round(dg / dlo, 2) if dlo > 0 else 99.0
        dwr = round(len(dw) / len(dt) * 100, 1)
        dp  = sum(t["pnl"] for t in dt)
        print(f"  {dr.upper():<10} {len(dt):>7} {dwr:>7} {dpf:>7} {f'${dp:.2f}':>11}")

    print(f"\n{SEP}")
    print(f"  PER D1-REGIME BREAKDOWN")
    print(f"  {'Regime':<12} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*48}")
    for rg in ("bull", "bear", "range", "compress"):
        rt = reg_map.get(rg, [])
        if not rt:
            continue
        rw  = [t for t in rt if t["pnl"] > 0]
        rl  = [t for t in rt if t["pnl"] <= 0]
        rg_ = sum(t["pnl"] for t in rw)
        rlo = abs(sum(t["pnl"] for t in rl))
        rpf = round(rg_ / rlo, 2) if rlo > 0 else 99.0
        rwr = round(len(rw) / len(rt) * 100, 1)
        rp  = sum(t["pnl"] for t in rt)
        print(f"  {rg.upper():<12} {len(rt):>7} {rwr:>7} {rpf:>7} {f'${rp:.2f}':>11}")

    print(f"\n{SEP}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"  {'Month':<10} {'Trades':>7} {'WR%':>7} {'P&L':>11} {'Balance':>10}")
    print(f"  {'─'*50}")
    running_bal  = INIT_BAL
    prof_months  = 0
    total_months = 0
    for month in sorted(monthly.keys()):
        mt   = monthly[month]
        mw   = sum(1 for t in mt if t["pnl"] > 0)
        mp   = sum(t["pnl"] for t in mt)
        mwr  = round(mw / len(mt) * 100, 1)
        running_bal += mp
        flag = "▲" if mp >= 0 else "▼"
        if mp >= 0:
            prof_months += 1
        total_months += 1
        print(f"  {month:<10} {len(mt):>7} {mwr:>7} {flag}${abs(mp):<9.2f} ${running_bal:.2f}")

    pm_pct = round(prof_months / total_months * 100, 1) if total_months > 0 else 0.0
    print(f"\n  Profitable months : {prof_months}/{total_months}  ({pm_pct}%)")
    print(f"{SEP2}\n")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═" * 70)
    print("  STRATEGY H — 2-YEAR BACKTEST")
    print("  Fetching M15 XAUUSD data from MT5...")
    print("═" * 70)

    bars = fetch_m15("XAUUSD", years=2)

    print("\nRunning simulation...")
    closed, final_bal, max_dd = run_simulation(bars)

    print_stats(closed, final_bal, max_dd)

    # Save trade log
    if closed:
        data_dir  = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(data_dir, exist_ok=True)
        out_path  = os.path.join(data_dir, "backtest_h_trades.csv")
        all_keys  = list(closed[0].keys())
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(closed)
        print(f"  Trade log saved : {out_path}")

    # Save summary text
    import io, contextlib
    summary_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "backtest_h_summary.txt"
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_stats(closed, final_bal, max_dd)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    print(f"  Summary saved   : {summary_path}")
