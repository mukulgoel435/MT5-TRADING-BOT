#!/usr/bin/env python3
"""
scripts/backtest_h_multi.py — Strategy H Multi-Instrument Backtest
==================================================================
Runs Strategy H (ISF) on XAUUSD + 3 forex pairs and prints a side-by-side
comparison so we know which instruments suit the strategy best.

Instruments:
  XAUUSD — Gold (baseline)
  EURUSD — Euro/Dollar (London/NY session flow)
  GBPUSD — Cable (strongest London session moves)
  USDJPY — Dollar/Yen (Tokyo + NY overlap)

Usage:
    py -3 scripts/backtest_h_multi.py
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

# ── Instrument configurations ────────────────────────────────────────────────
#  pip_size   : price units per pip (0.1 for gold, 0.0001 for EUR/GBP, 0.01 for JPY)
#  pip_val    : USD per pip per standard lot (100k units)
#  spread_pts : typical spread cost at entry in price units
#  slippage   : slippage in price units
#  session    : (start_hour, end_hour) UTC — hours to scan (24/7 for metals)

INSTRUMENTS = {
    "XAUUSD": {
        "pip_size":   0.1,
        "pip_val":    10.0,
        "spread_pts": 0.15,     # 1.5 pip spread
        "slippage":   0.05,
        "session":    (0, 24),  # 24/7
        "label":      "Gold",
    },
    "EURUSD": {
        "pip_size":   0.0001,
        "pip_val":    10.0,
        "spread_pts": 0.00012,  # 1.2 pip spread
        "slippage":   0.00003,
        "session":    (6, 21),  # London + NY only
        "label":      "EURUSD",
    },
    "GBPUSD": {
        "pip_size":   0.0001,
        "pip_val":    10.0,
        "spread_pts": 0.00018,  # 1.8 pip spread
        "slippage":   0.00003,
        "session":    (6, 21),  # London + NY only
        "label":      "GBPUSD",
    },
    "USDJPY": {
        "pip_size":   0.01,
        "pip_val":    9.0,      # approx $9 per pip at ~145 JPY/USD
        "spread_pts": 0.015,    # 1.5 pip spread
        "slippage":   0.005,
        "session":    (0, 24),  # Tokyo + London + NY
        "label":      "USDJPY",
    },
}

# ── Shared simulation parameters ──────────────────────────────────────────────
INIT_BAL         = 1000.0   # $1,000 account (matches live account)
RISK_PCT         = 0.01     # 1% per trade
RISK_PCT_REDUCED = 0.005    # 0.5% after 3 consecutive losses
DAILY_LOSS_LIMIT = 0.02     # halt day after −2% daily
CONSEC_REDUCE    = 3
CONSEC_HALT      = 6
ATR_P            = 14
ATR_AP           = 20
WARMUP           = 250
PATTERN_A_EXPIRE = 4
PATTERN_BC_EXPIRE = 1
FLOAT_EPS        = 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, p: int = 14) -> float:
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


def _pnl(direction: str, entry: float, exit_p: float, lot: float,
         pip_size: float, pip_val: float) -> float:
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / pip_size * pip_val * lot, 4)


def _pnl_r(direction: str, entry: float, sl: float, exit_p: float) -> float:
    sl_dist = abs(entry - sl)
    if sl_dist < FLOAT_EPS:
        return 0.0
    profit_pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(profit_pts / sl_dist, 3)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_m15(symbol: str, years: int = 2) -> pd.DataFrame:
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=years * 366 + 10)

    rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, start_dt, end_dt)
    if rates is None or len(rates) < 500:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 99999)

    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M15 data for {symbol}.")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]

    cutoff = df.index[-1] - pd.Timedelta(days=years * 366)
    df = df[df.index >= cutoff].copy()
    return df


# ── Simulation ────────────────────────────────────────────────────────────────

def run_simulation(bars: pd.DataFrame, inst: dict) -> tuple:
    """
    Run Strategy H simulation on `bars` using instrument config `inst`.
    Returns (closed_trades, final_balance, max_dd_pct).
    """
    pip_size    = inst["pip_size"]
    pip_val     = inst["pip_val"]
    spread_pts  = inst["spread_pts"]
    slippage    = inst["slippage"]
    sess_start  = inst["session"][0]
    sess_end    = inst["session"][1]

    n         = len(bars)
    balance   = INIT_BAL
    peak_bal  = INIT_BAL
    max_dd    = 0.0

    position  = None
    pending   = None
    closed    = []
    daily_pnl = 0.0
    daily_date = None
    consec_losses = 0
    halt_cooldown_bar = 0

    d1_regime  = D1_RANGE
    h4_dir     = H4_NONE
    daily_bias = BIAS_NEUTRAL

    ab_h = -1e18;  ab_l = 1e18
    asian_h = None;  asian_l = None
    pdh = None;  pdl = None;  pdc = None
    d1rh = None; d1rl = None
    last_ctx_date = None

    OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4_all = bars.resample("4h").agg(OHLCV_AGG).dropna()

    for i in range(WARMUP, n):
        bar = bars.iloc[i]
        t   = bar.name
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)

        hour = t.hour
        date = t.date()
        h_   = float(bar["high"])
        l_   = float(bar["low"])
        c_   = float(bar["close"])
        o_   = float(bar["open"])

        atr_window = bars.iloc[max(0, i - ATR_P - 5): i]
        atr        = _atr(atr_window, ATR_P)
        atr_avg    = _atr(atr_window, min(ATR_AP, len(atr_window)))

        if date != daily_date:
            daily_date = date
            daily_pnl  = 0.0

        # Asian range accumulation
        if hour >= 22 or hour < 7:
            ab_h = max(ab_h, h_)
            ab_l = min(ab_l, l_)

        # Daily context at 07:00 UTC
        if hour == 7 and date != last_ctx_date:
            last_ctx_date = date
            if ab_h > ab_l and ab_h != -1e18:
                asian_h = ab_h
                asian_l = ab_l
            ab_h = -1e18;  ab_l = 1e18

            d1_before = d1_all[d1_all.index.date < date]
            if len(d1_before) >= 2:
                pdh = float(d1_before.iloc[-1]["high"])
                pdl = float(d1_before.iloc[-1]["low"])
                pdc = float(d1_before.iloc[-1]["close"])
                rec = d1_before.iloc[-min(20, len(d1_before)):]
                d1rh = float(rec["high"].max())
                d1rl = float(rec["low"].min())
            if len(d1_before) >= 12:
                d1_regime = compute_d1_regime(d1_before)

            h4_cutoff = pd.Timestamp(year=date.year, month=date.month,
                                     day=date.day, hour=7, tzinfo=timezone.utc)
            h4_before = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4_cutoff]
            if len(h4_before) >= 12:
                h4_dir = compute_h4_direction(h4_before)

            if d1rh is not None and d1rl is not None:
                daily_bias, _ = compute_daily_bias(
                    d1_regime, h4_dir, c_, d1rh, d1rl, pdh, pdl, pdc,
                )

        # ── Manage open position ──────────────────────────────────────────────
        if position is not None:
            pos       = position
            direction = pos["direction"]
            bars_held = i - pos["open_bar"]

            # Update best price for ATR trail
            if direction == "long":
                pos["best_price"] = max(pos.get("best_price", pos["entry_price"]), h_)
            else:
                pos["best_price"] = min(pos.get("best_price", pos["entry_price"]), l_)

            sl_hit  = check_sl_hit(l_, h_, pos["current_sl"], direction)
            tp1_hit = (not pos["tp1_reached"] and
                       check_tp1_hit(l_, h_, pos["tp1"], direction))

            if sl_hit:
                fill = (pos["current_sl"] - slippage if direction == "long"
                        else pos["current_sl"] + slippage)
                remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
                pnl = _pnl(direction, pos["entry_price"], fill, remaining, pip_size, pip_val)
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
                    "exit_price":    fill,
                    "exit_reason":   "SL_HIT",
                    "pnl":           pnl,
                    "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                    "bars_held":     bars_held,
                    "result":        result,
                    "balance_after": balance,
                })
                position = None
                pending  = None
                continue

            if tp1_hit:
                half    = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(direction, pos["entry_price"], pos["tp1"], half, pip_size, pip_val)
                pos["tp1_reached"] = True
                pos["tp1_pnl"]     = tp1_pnl
                pos["half_lot"]    = half
                pos["current_sl"]  = pos["entry_price"]
                continue

            if pos["tp1_reached"]:
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                new_trail = update_trail_sl(pos, m15_w, atr,
                                            current_price=pos["best_price"])
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail

                if check_regime_exit(pos, d1_regime, h4_dir):
                    fill = c_ - slippage if direction == "long" else c_ + slippage
                    pnl  = (_pnl(direction, pos["entry_price"], fill,
                                 pos["half_lot"], pip_size, pip_val)
                            + pos["tp1_pnl"])
                    balance   += pnl
                    daily_pnl += pnl
                    peak_bal   = max(peak_bal, balance)
                    max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                    result     = "win" if pnl > 0 else "loss"
                    consec_losses = 0 if result == "win" else consec_losses + 1
                    closed.append({
                        **pos,
                        "exit_price":    fill,
                        "exit_reason":   "REGIME_EXIT",
                        "pnl":           pnl,
                        "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                        "bars_held":     bars_held,
                        "result":        result,
                        "balance_after": balance,
                    })
                    position = None
                    continue

            if check_time_stop(bars_held, pos["tp1_reached"]):
                fill = c_ - slippage if direction == "long" else c_ + slippage
                pnl  = _pnl(direction, pos["entry_price"], fill, pos["lot"], pip_size, pip_val)
                balance   += pnl
                daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                result     = "win" if pnl > 0 else "loss"
                consec_losses = 0 if result == "win" else consec_losses + 1
                closed.append({
                    **pos,
                    "exit_price":    fill,
                    "exit_reason":   "TIME_STOP",
                    "pnl":           pnl,
                    "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                    "bars_held":     bars_held,
                    "result":        result,
                    "balance_after": balance,
                })
                position = None
                continue

        # ── Fill pending order ────────────────────────────────────────────────
        if pending is not None and position is None:
            p         = pending
            direction = p["direction"]

            if i > p["expires_bar"]:
                pending = None
            else:
                filled     = False
                fill_price = None
                if p["order_type"] == "market":
                    filled     = True
                    fill_price = o_ + slippage if direction == "long" else o_ - slippage
                elif p["order_type"] == "limit":
                    ep = p["entry_price"]
                    if direction == "long" and l_ <= ep:
                        filled = True;  fill_price = ep + slippage
                    elif direction == "short" and h_ >= ep:
                        filled = True;  fill_price = ep - slippage

                if filled and fill_price is not None:
                    fill_price += spread_pts / 2 if direction == "long" else -spread_pts / 2
                    fill_price  = round(fill_price, 6)
                    sl  = compute_sl(fill_price, direction, p["atr"], p.get("sweep_extreme"))
                    tp1 = compute_tp1(fill_price, sl, direction)
                    if not sl_distance_valid(fill_price, sl, p["atr"]):
                        pending = None
                    else:
                        position = {
                            "direction":     direction,
                            "entry_price":   fill_price,
                            "sl":            sl,
                            "tp1":           tp1,
                            "current_sl":    sl,
                            "lot":           p["lot"],
                            "open_bar":      i,
                            "tp1_reached":   False,
                            "tp1_pnl":       0.0,
                            "half_lot":      max(0.01, round(p["lot"] * 0.5, 2)),
                            "pattern":       p["pattern"],
                            "atr_at_entry":  p["atr"],
                            "entry_time":    str(t),
                            "d1_regime":     d1_regime,
                            "h4_dir":        h4_dir,
                            "daily_bias":    daily_bias,
                            "session":       p.get("session", "?"),
                            "balance_before": balance,
                            "sl_distance":   abs(fill_price - sl),
                            "best_price":    fill_price,
                        }
                        pending = None

        # ── Detect new patterns ───────────────────────────────────────────────
        if position is not None or pending is not None:
            continue

        if not (sess_start <= hour < sess_end):
            continue
        if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
            continue
        if consec_losses >= CONSEC_HALT:
            if halt_cooldown_bar == 0:
                halt_cooldown_bar = i + 96
            if i < halt_cooldown_bar:
                continue
            else:
                consec_losses = CONSEC_REDUCE
                halt_cooldown_bar = 0
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue
        if atr_avg > 0:
            if atr > 3.0 * atr_avg or atr < 0.4 * atr_avg:
                continue

        risk_pct   = RISK_PCT_REDUCED if consec_losses >= CONSEC_REDUCE else RISK_PCT
        m15_window = bars.iloc[max(0, i - 60): i + 1]

        for direction in ("long", "short"):
            if not bias_allows_direction(daily_bias, direction):
                continue

            setups = []
            pc = detect_pattern_c(m15_window, direction, atr)
            if pc:
                setups.append(pc)
            pa = detect_pattern_a(m15_window, hour, direction,
                                   asian_h, asian_l, pdh, pdl, atr)
            if pa:
                setups.append(pa)
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
            order_type  = "limit" if setup["type"] == "A" else "market"
            expires_bar = i + (PATTERN_A_EXPIRE if setup["type"] == "A" else PATTERN_BC_EXPIRE)

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
            break

    # Force-close any open position at final bar
    if position is not None:
        pos       = position
        direction = pos["direction"]
        fill      = float(bars.iloc[-1]["close"])
        remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
        pnl       = _pnl(direction, pos["entry_price"], fill, remaining, pip_size, pip_val)
        if pos["tp1_reached"]:
            pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({
            **pos,
            "exit_price":    fill,
            "exit_reason":   "FORCE_CLOSE",
            "pnl":           pnl,
            "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
            "bars_held":     n - 1 - pos["open_bar"],
            "result":        "win" if pnl > 0 else "win",
            "balance_after": balance,
        })

    return closed, balance, max_dd


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(closed: list, final_balance: float, max_dd: float) -> dict:
    if not closed:
        return {"trades": 0, "win_rate": 0, "pf": 0, "net": 0, "ret": 0,
                "max_dd": max_dd, "avg_w": 0, "avg_l": 0, "exp_r": 0,
                "pat_b": 0, "pat_c": 0, "pat_a": 0, "monthly_wins": 0}

    total = len(closed)
    wins  = [t for t in closed if t["pnl"] > 0]
    losses= [t for t in closed if t["pnl"] <= 0]
    gw    = sum(t["pnl"] for t in wins)
    gl    = abs(sum(t["pnl"] for t in losses))

    monthly = defaultdict(list)
    for t in closed:
        et = str(t.get("entry_time", ""))[:7]
        if et:
            monthly[et].append(t)
    monthly_wins = sum(1 for m in monthly.values()
                       if sum(t["pnl"] for t in m) > 0)

    pat_counts = defaultdict(int)
    pat_wins   = defaultdict(int)
    for t in closed:
        p = t.get("pattern", "?")
        pat_counts[p] += 1
        if t["pnl"] > 0:
            pat_wins[p] += 1

    return {
        "trades":       total,
        "win_rate":     round(len(wins) / total * 100, 1),
        "pf":           round(gw / gl, 2) if gl > 0 else 99.0,
        "net":          round(gw - gl, 2),
        "ret":          round((final_balance - INIT_BAL) / INIT_BAL * 100, 1),
        "max_dd":       round(max_dd, 1),
        "avg_w":        round(gw / len(wins), 2) if wins else 0,
        "avg_l":        round(gl / len(losses), 2) if losses else 0,
        "exp_r":        round(sum(t.get("pnl_r", 0) for t in closed) / total, 3),
        "monthly_wins": f"{monthly_wins}/{len(monthly)}",
        "pat_B":        f"{pat_counts['B']}({round(pat_wins['B']/pat_counts['B']*100,0):.0f}%)" if pat_counts['B'] else "0",
        "pat_C":        f"{pat_counts['C']}({round(pat_wins['C']/pat_counts['C']*100,0):.0f}%)" if pat_counts['C'] else "0",
        "pat_A":        f"{pat_counts['A']}({round(pat_wins['A']/pat_counts['A']*100,0):.0f}%)" if pat_counts['A'] else "0",
    }


def save_trades_csv(closed: list, symbol: str, out_dir: str):
    if not closed:
        return
    path = os.path.join(out_dir, f"backtest_h_{symbol.lower()}.csv")
    fields = ["entry_time", "direction", "pattern", "session",
              "entry_price", "sl", "tp1", "exit_price",
              "lot", "sl_distance", "pnl", "pnl_r",
              "result", "exit_reason", "bars_held", "tp1_reached",
              "d1_regime", "h4_dir", "daily_bias",
              "balance_before", "balance_after"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(closed)
    print(f"  Saved: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    SEP  = "-" * 78
    SEP2 = "=" * 78

    print(f"\n{SEP2}")
    print(f"  STRATEGY H -- MULTI-INSTRUMENT BACKTEST  |  2-Year M15")
    print(f"  Starting balance: ${INIT_BAL:.0f}  |  Risk: {RISK_PCT*100:.0f}%/trade")
    print(f"{SEP2}\n")

    if not mt5.initialize(login=config.LOGIN, password=config.PASSWORD,
                          server=config.SERVER):
        print(f"  MT5 init failed: {mt5.last_error()}")
        return

    out_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(out_dir, exist_ok=True)

    results = {}

    for symbol, inst in INSTRUMENTS.items():
        print(f"\n{'-'*50}")
        print(f"  Running: {symbol} ({inst['label']})")
        print(f"{'-'*50}")
        try:
            bars = fetch_m15(symbol, years=2)
            print(f"  M15 bars: {len(bars):,}  |  {bars.index[0].date()} to {bars.index[-1].date()}")
            print(f"  Simulating...", end="", flush=True)
            closed, final_bal, max_dd = run_simulation(bars, inst)
            print(f" done. {len(closed)} trades.")
            stats = compute_stats(closed, final_bal, max_dd)
            results[symbol] = stats
            save_trades_csv(closed, symbol, out_dir)
        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()
            results[symbol] = None

    mt5.shutdown()

    # ── Comparison table ──────────────────────────────────────────────────────
    print(f"\n\n{SEP2}")
    print(f"  COMPARISON TABLE  |  Strategy H ISF  |  2-Year M15  |  ${INIT_BAL:.0f} account")
    print(f"{SEP2}")
    print(f"  {'Metric':<22}", end="")
    for sym in INSTRUMENTS:
        print(f"  {sym:>12}", end="")
    print()
    print(f"  {'-'*78}")

    rows = [
        ("Trades (2yr)",  "trades",       ""),
        ("Win Rate",       "win_rate",     "%"),
        ("Profit Factor",  "pf",           ""),
        ("Net P&L",        "net",          "$"),
        ("Return",         "ret",          "%"),
        ("Max Drawdown",   "max_dd",       "%"),
        ("Avg Win",        "avg_w",        "$"),
        ("Avg Loss",       "avg_l",        "$"),
        ("Expectancy",     "exp_r",        "R"),
        ("Prof. Months",   "monthly_wins", ""),
        ("Pattern B",      "pat_B",        ""),
        ("Pattern C",      "pat_C",        ""),
        ("Pattern A",      "pat_A",        ""),
    ]

    for label, key, suffix in rows:
        print(f"  {label:<22}", end="")
        for sym in INSTRUMENTS:
            s = results.get(sym)
            if s is None:
                print(f"  {'ERROR':>12}", end="")
            else:
                val = s.get(key, "N/A")
                if isinstance(val, float):
                    cell = f"{val:+.1f}{suffix}" if suffix in ("$", "%") else f"{val:.2f}{suffix}"
                else:
                    cell = f"{val}{suffix}"
                print(f"  {cell:>12}", end="")
        print()

    print(f"\n{SEP2}")
    print(f"  RANKING (by Profit Factor)\n")

    ranked = [(sym, r) for sym, r in results.items() if r is not None]
    ranked.sort(key=lambda x: x[1]["pf"], reverse=True)
    for rank, (sym, r) in enumerate(ranked, 1):
        verdict = "[ADD to live]" if r["pf"] >= 1.3 else ("[Consider]" if r["pf"] >= 1.1 else "[Skip]")
        print(f"  #{rank} {sym:<8}  PF={r['pf']:.2f}  WR={r['win_rate']}%  "
              f"Ret={r['ret']:+.1f}%  MaxDD={r['max_dd']:.1f}%  => {verdict}")

    print(f"\n  Recommendation:")
    live_candidates = [sym for sym, r in ranked if r is not None and r["pf"] >= 1.3]
    if live_candidates:
        print(f"  Add to live scanner: {', '.join(live_candidates)}")
    else:
        print(f"  No forex pair meets PF >= 1.3 -- review thresholds before going live.")
    print(f"{SEP2}\n")


if __name__ == "__main__":
    main()
