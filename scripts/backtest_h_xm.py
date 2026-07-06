#!/usr/bin/env python3
"""
scripts/backtest_h_xm.py  --  Strategy H on XM data: GOLD + BTCUSD
=====================================================================
Runs two full backtests using max available data from XM terminal:
  1. GOLD  (XM's XAUUSD, 24/7 all weekday sessions)
  2. BTCUSD (24/7 incl. weekends, all sessions)

Usage:
    py -3 scripts/backtest_h_xm.py
    py -3 scripts/backtest_h_xm.py --symbol GOLD
    py -3 scripts/backtest_h_xm.py --symbol BTCUSD

Outputs per symbol:
  data/backtest_h_xm_GOLD_trades.csv
  data/backtest_h_xm_BTCUSD_trades.csv
  data/backtest_h_xm_summary.txt
"""

import sys, os, csv, io, contextlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import MetaTrader5 as mt5
import config_xm as cfg

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_NONE, BIAS_NEUTRAL,
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

XM_PATH = r"C:\Program Files\XM Global MT5\terminal64.exe"

# ── Per-symbol configs ────────────────────────────────────────────────────────
SYMBOL_CONFIGS = {
    "GOLD": {
        "pip_size":     0.1,
        "pip_val":      10.0,
        "spread_cost":  0.30,   # half-spread in price units (~$0.30 on $0.56 spread)
        "session_start": 0,     # UTC hour -- 24/7 all weekday sessions
        "session_end":  24,
        "ctx_hour":     0,      # daily context update at midnight (24/7 mode)
        "asian_build":  False,  # no separate Asian range -- using full 24h range
        "init_bal":     1000.0,
        "label":        "GOLD (XM) - 24/7 all weekday sessions (tuned)",
        # GOLD-specific tuning
        "regime_dir_filter": True,   # BULL → longs only; BEAR → shorts only; RANGE → both
        "halt_reset_zero":   True,   # reset consec to 0 after halt (prevents infinite halt loop)
        "daily_loss_limit":  0.015,  # 1.5% per day (tighter than default 2%)
        "atr_low_factor":    0.5,    # skip if atr < 0.5 * atr_avg (was 0.4)
        "tp1_rr":            1.5,    # TP1 at 1.5:1 instead of 1:1 — lock in more on first partial
        "pattern_b_only":    True,   # skip Pattern C (PF=1.06 barely positive), keep only B
    },
    "BTCUSD": {
        "pip_size":     1.0,
        "pip_val":      1.0,
        "spread_cost":  50.0,   # half-spread in price units ($50 on $50 spread)
        "session_start": 0,     # 24/7
        "session_end":  24,
        "ctx_hour":     0,      # daily context update at midnight
        "asian_build":  False,  # BTC 24/7 — use prior 24h range instead
        "init_bal":     1000.0,
        "label":        "BTCUSD (XM) - 24/7 incl. weekends",
    },
}

# ── Shared constants ──────────────────────────────────────────────────────────
RISK_PCT         = 0.01
RISK_PCT_REDUCED = 0.005
SLIPPAGE         = 0.05        # pts on fills

ATR_P   = 14
ATR_AP  = 20
WARMUP  = 250

DAILY_LOSS_LIMIT  = 0.02
CONSEC_REDUCE     = 3
CONSEC_HALT       = 6
PATTERN_A_EXPIRE  = 4
PATTERN_BC_EXPIRE = 1
FLOAT_EPS         = 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr(df, p=14):
    if len(df) < 2:
        return 1.0
    h = df["high"].values[-p - 2:]
    l = df["low"].values[-p - 2:]
    c = df["close"].values[-p - 2:]
    if len(h) < 2:
        return max(float(h[-1] - l[-1]), FLOAT_EPS)
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    use = tr[-min(p, len(tr)):]
    return float(use.mean()) if len(use) > 0 else 1.0


def _pnl(direction, entry, exit_p, lot, pip_size, pip_val):
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / pip_size * pip_val * lot, 4)


def _pnl_r(direction, entry, sl, exit_p):
    sl_dist = abs(entry - sl)
    if sl_dist < FLOAT_EPS:
        return 0.0
    profit_pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(profit_pts / sl_dist, 3)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_bars(symbol: str) -> pd.DataFrame:
    if not mt5.initialize(login=cfg.LOGIN, password=cfg.PASSWORD,
                          server=cfg.SERVER, path=XM_PATH):
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 99999)
    if rates is None or len(rates) < 500:
        raise RuntimeError(f"Not enough data for {symbol}: {len(rates) if rates else 0} bars")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]
    print(f"  M15 bars loaded : {len(df):,}")
    print(f"  Period          : {df.index[0].date()}  ->  {df.index[-1].date()}")
    return df


# ── Simulation ────────────────────────────────────────────────────────────────

def run_simulation(bars: pd.DataFrame, cfg_sym: dict) -> tuple:
    PIP_SIZE    = cfg_sym["pip_size"]
    PIP_VAL     = cfg_sym["pip_val"]
    SPREAD_COST = cfg_sym["spread_cost"]
    SES_START   = cfg_sym["session_start"]
    SES_END     = cfg_sym["session_end"]
    CTX_HOUR    = cfg_sym["ctx_hour"]
    INIT_BAL    = cfg_sym["init_bal"]
    ASIAN_BUILD = cfg_sym["asian_build"]
    REGIME_DIR_FILTER = cfg_sym.get("regime_dir_filter", False)
    HALT_RESET_ZERO   = cfg_sym.get("halt_reset_zero", False)
    DAILY_LOSS_LIM    = cfg_sym.get("daily_loss_limit", DAILY_LOSS_LIMIT)
    ATR_LOW_FACTOR    = cfg_sym.get("atr_low_factor", 0.4)
    TP1_RR            = cfg_sym.get("tp1_rr", 1.0)
    PATTERN_B_ONLY    = cfg_sym.get("pattern_b_only", False)

    n         = len(bars)
    balance   = INIT_BAL
    peak_bal  = INIT_BAL
    max_dd    = 0.0

    position  = None
    pending   = None
    closed    = []

    daily_pnl     = 0.0
    daily_date    = None
    consec_losses = 0
    halt_cooldown = 0

    d1_regime  = D1_RANGE
    h4_dir     = H4_NONE
    daily_bias = BIAS_NEUTRAL

    ab_h = -1e18; ab_l = 1e18
    asian_h = asian_l = None
    pdh = pdl = pdc = d1rh = d1rl = None
    last_ctx_date = None

    OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4_all = bars.resample("4h").agg(OHLCV_AGG).dropna()
    print(f"  D1 bars         : {len(d1_all)}")
    print(f"  H4 bars         : {len(h4_all)}")

    for i in range(WARMUP, n):
        bar  = bars.iloc[i]
        t    = bar.name
        if hasattr(t, "tzinfo") and t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)

        hour = t.hour
        date = t.date()
        o_   = float(bar["open"])
        h_   = float(bar["high"])
        l_   = float(bar["low"])
        c_   = float(bar["close"])

        atr_window = bars.iloc[max(0, i - ATR_P - 5): i]
        atr        = _atr(atr_window, ATR_P)
        atr_avg    = _atr(atr_window, min(ATR_AP, len(atr_window)))

        if date != daily_date:
            daily_date = date
            daily_pnl  = 0.0

        # Asian range accumulation (GOLD only — 22:00-06:59)
        if ASIAN_BUILD:
            if hour >= 22 or hour < 7:
                ab_h = max(ab_h, h_)
                ab_l = min(ab_l, l_)

        # Daily context update
        if hour == CTX_HOUR and date != last_ctx_date:
            last_ctx_date = date

            if ASIAN_BUILD:
                if ab_h > ab_l and ab_h != -1e18:
                    asian_h = ab_h; asian_l = ab_l
                ab_h = -1e18; ab_l = 1e18
            else:
                # BTC: use prior 24h range as "asian" reference
                prev_day_bars = bars.iloc[max(0, i - 96): i]
                if len(prev_day_bars) >= 10:
                    asian_h = float(prev_day_bars["high"].max())
                    asian_l = float(prev_day_bars["low"].min())

            d1_before = d1_all[d1_all.index.date < date]
            if len(d1_before) >= 2:
                pdh  = float(d1_before.iloc[-1]["high"])
                pdl  = float(d1_before.iloc[-1]["low"])
                pdc  = float(d1_before.iloc[-1]["close"])
                rec  = d1_before.iloc[-min(20, len(d1_before)):]
                d1rh = float(rec["high"].max())
                d1rl = float(rec["low"].min())
            if len(d1_before) >= 12:
                d1_regime = compute_d1_regime(d1_before)
            h4_cut = pd.Timestamp(year=date.year, month=date.month,
                                   day=date.day, hour=CTX_HOUR, tzinfo=timezone.utc)
            h4_before = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4_cut]
            if len(h4_before) >= 12:
                h4_dir = compute_h4_direction(h4_before)
            if d1rh is not None and d1rl is not None:
                daily_bias, _ = compute_daily_bias(d1_regime, h4_dir, c_,
                                                    d1rh, d1rl, pdh, pdl, pdc)

        # ── Manage open position ──────────────────────────────────────────────
        if position is not None:
            pos       = position
            direction = pos["direction"]
            bars_held = i - pos["open_bar"]

            sl_hit  = check_sl_hit(l_, h_, pos["current_sl"], direction)
            tp1_hit = not pos["tp1_reached"] and check_tp1_hit(l_, h_, pos["tp1"], direction)

            if sl_hit:
                fill      = (pos["current_sl"] - SLIPPAGE if direction == "long"
                             else pos["current_sl"] + SLIPPAGE)
                remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
                pnl       = _pnl(direction, pos["entry_price"], fill, remaining, PIP_SIZE, PIP_VAL)
                if pos["tp1_reached"]:
                    pnl += pos["tp1_pnl"]
                balance   += pnl; daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                result     = "win" if pnl > 0 else "loss"
                consec_losses = 0 if result == "win" else consec_losses + 1
                closed.append({**pos, "exit_price": fill, "exit_reason": "SL_HIT",
                                "pnl": pnl, "pnl_r": _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                                "bars_held": bars_held, "result": result, "balance_after": balance})
                position = None; pending = None
                continue

            if tp1_hit:
                half    = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(direction, pos["entry_price"], pos["tp1"], half, PIP_SIZE, PIP_VAL)
                pos["tp1_reached"] = True
                pos["tp1_pnl"]     = tp1_pnl
                pos["half_lot"]    = half
                pos["current_sl"]  = pos["entry_price"]
                pos.setdefault("best_price", pos["entry_price"])
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                new_trail = update_trail_sl(pos, m15_w, atr)
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail
                continue

            if pos["tp1_reached"]:
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                # update best_price before trail computation
                if direction == "long":
                    pos["best_price"] = max(pos.get("best_price", pos["entry_price"]), h_)
                else:
                    pos["best_price"] = min(pos.get("best_price", pos["entry_price"]), l_)
                new_trail = update_trail_sl(pos, m15_w, atr)
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail

                if check_regime_exit(pos, d1_regime, h4_dir):
                    fill  = c_ - SLIPPAGE if direction == "long" else c_ + SLIPPAGE
                    pnl   = (_pnl(direction, pos["entry_price"], fill, pos["half_lot"], PIP_SIZE, PIP_VAL)
                             + pos["tp1_pnl"])
                    balance   += pnl; daily_pnl += pnl
                    peak_bal   = max(peak_bal, balance)
                    max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                    result     = "win" if pnl > 0 else "loss"
                    consec_losses = 0 if result == "win" else consec_losses + 1
                    closed.append({**pos, "exit_price": fill, "exit_reason": "REGIME_EXIT",
                                    "pnl": pnl, "pnl_r": _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                                    "bars_held": bars_held, "result": result, "balance_after": balance})
                    position = None
                    continue

            if check_time_stop(bars_held, pos["tp1_reached"]):
                fill  = c_ - SLIPPAGE if direction == "long" else c_ + SLIPPAGE
                pnl   = _pnl(direction, pos["entry_price"], fill, pos["lot"], PIP_SIZE, PIP_VAL)
                balance   += pnl; daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                result     = "win" if pnl > 0 else "loss"
                consec_losses = 0 if result == "win" else consec_losses + 1
                closed.append({**pos, "exit_price": fill, "exit_reason": "TIME_STOP",
                                "pnl": pnl, "pnl_r": _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                                "bars_held": bars_held, "result": result, "balance_after": balance})
                position = None
                continue

        # ── Fill pending order ────────────────────────────────────────────────
        if pending is not None and position is None:
            p = pending
            direction = p["direction"]
            if i > p["expires_bar"]:
                pending = None
            else:
                filled = False; fill_price = None
                if p["order_type"] == "market":
                    filled     = True
                    fill_price = o_ + SLIPPAGE if direction == "long" else o_ - SLIPPAGE
                elif p["order_type"] == "limit":
                    ep = p["entry_price"]
                    if direction == "long" and l_ <= ep:
                        filled = True; fill_price = ep + SLIPPAGE
                    elif direction == "short" and h_ >= ep:
                        filled = True; fill_price = ep - SLIPPAGE

                if filled and fill_price is not None:
                    fill_price += SPREAD_COST / 2 if direction == "long" else -SPREAD_COST / 2
                    fill_price  = round(fill_price, 5)
                    sl  = compute_sl(fill_price, direction, p["atr"], p.get("sweep_extreme"))
                    sl_dist = abs(fill_price - sl)
                    tp1 = (fill_price + TP1_RR * sl_dist if direction == "long"
                           else fill_price - TP1_RR * sl_dist)
                    if not sl_distance_valid(fill_price, sl, p["atr"]):
                        pending = None
                    else:
                        position = {
                            "direction":     direction,
                            "entry_price":   fill_price,
                            "sl":            sl, "tp1": tp1, "current_sl": sl,
                            "lot":           p["lot"], "open_bar": i,
                            "tp1_reached":   False, "tp1_pnl": 0.0,
                            "half_lot":      max(0.01, round(p["lot"] * 0.5, 2)),
                            "pattern":       p["pattern"],
                            "atr_at_entry":  p["atr"],
                            "entry_time":    str(t),
                            "d1_regime":     d1_regime, "h4_dir": h4_dir,
                            "daily_bias":    daily_bias,
                            "session":       p.get("session", "?"),
                            "balance_before": balance,
                            "sl_distance":   abs(fill_price - sl),
                            "weekday":       t.weekday(),
                        }
                        pending = None

        # ── Pattern detection ─────────────────────────────────────────────────
        if position is not None or pending is not None:
            continue

        if not (SES_START <= hour < SES_END):
            continue
        if daily_pnl < -(balance * DAILY_LOSS_LIM):
            continue
        if consec_losses >= CONSEC_HALT:
            if halt_cooldown == 0:
                halt_cooldown = i + 96
            if i < halt_cooldown:
                continue
            else:
                consec_losses = 0 if HALT_RESET_ZERO else CONSEC_REDUCE
                halt_cooldown = 0
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue
        if atr_avg > 0 and (atr > 3.0 * atr_avg or atr < ATR_LOW_FACTOR * atr_avg):
            continue

        risk_pct   = RISK_PCT_REDUCED if consec_losses >= CONSEC_REDUCE else RISK_PCT
        m15_window = bars.iloc[max(0, i - 60): i + 1]

        for direction in ("long", "short"):
            if not bias_allows_direction(daily_bias, direction):
                continue
            if REGIME_DIR_FILTER:
                if d1_regime == D1_BULL and direction == "short":
                    continue
                if d1_regime == D1_BEAR and direction == "long":
                    continue

            setups = []
            if not PATTERN_B_ONLY:
                pc = detect_pattern_c(m15_window, direction, atr)
                if pc:
                    setups.append(pc)
            pa = detect_pattern_a(m15_window, hour, direction, asian_h, asian_l, pdh, pdl, atr)
            if pa:
                setups.append(pa)
            pb = detect_pattern_b(m15_window, direction, atr)
            if pb:
                setups.append(pb)

            setup = pattern_priority(setups)
            if setup is None:
                continue

            test_entry = setup["entry_price"]
            sl_test    = compute_sl(test_entry, direction, atr, setup.get("sweep_extreme"))
            if not sl_distance_valid(test_entry, sl_test, atr):
                continue

            lot = compute_lot(balance, abs(test_entry - sl_test), risk_pct)
            if setup["type"] == "A":
                order_type  = "limit"
                expires_bar = i + PATTERN_A_EXPIRE
            else:
                order_type  = "market"
                expires_bar = i + PATTERN_BC_EXPIRE

            pending = {
                "direction":     direction,
                "entry_price":   test_entry,
                "sweep_extreme": setup.get("sweep_extreme"),
                "lot":           lot, "atr": atr,
                "pattern":       setup["type"],
                "order_type":    order_type,
                "expires_bar":   expires_bar,
                "session":       setup.get("session", "?"),
            }
            break

    # Force-close final bar
    if position is not None:
        pos  = position
        fill = float(bars.iloc[-1]["close"])
        remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
        pnl  = _pnl(pos["direction"], pos["entry_price"], fill, remaining, PIP_SIZE, PIP_VAL)
        if pos["tp1_reached"]:
            pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({**pos, "exit_price": fill, "exit_reason": "FORCE_CLOSE",
                        "pnl": pnl, "pnl_r": _pnl_r(pos["direction"], pos["entry_price"], pos["sl"], fill),
                        "bars_held": n - 1 - pos["open_bar"], "result": "win" if pnl > 0 else "loss",
                        "balance_after": balance})

    return closed, balance, max_dd


# ── Statistics ────────────────────────────────────────────────────────────────

def print_stats(closed, final_balance, max_dd, init_bal, label):
    SEP  = "-" * 68
    SEP2 = "=" * 68

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
    ret    = round((final_balance - init_bal) / init_bal * 100, 1)
    wr     = round(len(wins) / total * 100, 1)
    avg_w  = round(gw / len(wins), 2)   if wins   else 0.0
    avg_l  = round(gl / len(losses), 2) if losses else 0.0
    exp_r  = round(sum(t.get("pnl_r", 0) for t in closed) / total, 3)

    sl_hits    = sum(1 for t in closed if t.get("exit_reason") == "SL_HIT")
    time_stops = sum(1 for t in closed if t.get("exit_reason") == "TIME_STOP")
    regime_ex  = sum(1 for t in closed if t.get("exit_reason") == "REGIME_EXIT")
    tp1_hits   = sum(1 for t in closed if t.get("tp1_reached"))

    pat_map = defaultdict(list)
    dir_map = defaultdict(list)
    reg_map = defaultdict(list)
    monthly = defaultdict(list)
    wkday_map = defaultdict(list)   # 0=Mon...6=Sun

    for t in closed:
        pat_map[t.get("pattern", "?")].append(t)
        dir_map[t.get("direction", "?")].append(t)
        reg_map[t.get("d1_regime", "?")].append(t)
        et = str(t.get("entry_time", ""))[:7]
        if et:
            monthly[et].append(t)
        wd = t.get("weekday")
        if wd is not None:
            wkday_map[wd].append(t)

    print(f"\n{SEP2}")
    print(f"  STRATEGY H XM  --  {label}")
    print(f"{SEP2}")
    print(f"  Initial balance  : ${init_bal:.2f}")
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
    print(f"    SL Hit         : {sl_hits:>4}  ({sl_hits/total*100:.1f}%)")
    print(f"    Time Stop      : {time_stops:>4}  ({time_stops/total*100:.1f}%)")
    print(f"    Trail/Regime   : {regime_ex:>4}  ({regime_ex/total*100:.1f}%)")
    print(f"  TP1 reached      : {tp1_hits:>4}  ({tp1_hits/total*100:.1f}%)")

    print(f"\n{SEP}")
    print(f"  PER-PATTERN")
    print(f"  {'Pattern':<10} {'N':>5} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    for ptype in sorted(pat_map.keys()):
        pt = pat_map[ptype]
        pw = [t for t in pt if t["pnl"] > 0]
        pl = [t for t in pt if t["pnl"] <= 0]
        pg = sum(t["pnl"] for t in pw)
        plo = abs(sum(t["pnl"] for t in pl))
        ppf = round(pg / plo, 2) if plo > 0 else 99.0
        pwr = round(len(pw) / len(pt) * 100, 1)
        print(f"  Pattern {ptype:<4}  {len(pt):>5} {pwr:>7} {ppf:>7} {sum(t['pnl'] for t in pt):>+11.2f}")

    print(f"\n{SEP}")
    print(f"  PER-DIRECTION")
    print(f"  {'Dir':<10} {'N':>5} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    for dr in ("long", "short"):
        dt = dir_map.get(dr, [])
        if not dt:
            continue
        dw = [t for t in dt if t["pnl"] > 0]
        dl = [t for t in dt if t["pnl"] <= 0]
        dg  = sum(t["pnl"] for t in dw)
        dlo = abs(sum(t["pnl"] for t in dl))
        dpf = round(dg / dlo, 2) if dlo > 0 else 99.0
        dwr = round(len(dw) / len(dt) * 100, 1)
        print(f"  {dr.upper():<10} {len(dt):>5} {dwr:>7} {dpf:>7} {sum(t['pnl'] for t in dt):>+11.2f}")

    print(f"\n{SEP}")
    print(f"  PER D1-REGIME")
    print(f"  {'Regime':<12} {'N':>5} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
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
        print(f"  {rg.upper():<12} {len(rt):>5} {rwr:>7} {rpf:>7} {sum(t['pnl'] for t in rt):>+11.2f}")

    # Weekday breakdown
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    has_weekend = any(k in (5, 6) for k in wkday_map.keys())
    if has_weekend:
        print(f"\n{SEP}")
        print(f"  WEEKDAY BREAKDOWN (Sat/Sun = weekend BTC)")
        print(f"  {'Day':<6} {'N':>5} {'WR%':>7} {'Net P&L':>11}")
        for wd in range(7):
            wt = wkday_map.get(wd, [])
            if not wt:
                continue
            ww  = [t for t in wt if t["pnl"] > 0]
            wwr = round(len(ww) / len(wt) * 100, 1)
            print(f"  {DAY_NAMES[wd]:<6} {len(wt):>5} {wwr:>7} {sum(t['pnl'] for t in wt):>+11.2f}")

    print(f"\n{SEP}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"  {'Month':<10} {'N':>5} {'WR%':>7} {'P&L':>11} {'Balance':>10}")
    running_bal  = init_bal
    prof_months  = 0
    total_months = 0
    for month in sorted(monthly.keys()):
        mt   = monthly[month]
        mw   = sum(1 for t in mt if t["pnl"] > 0)
        mp   = sum(t["pnl"] for t in mt)
        mwr  = round(mw / len(mt) * 100, 1)
        running_bal += mp
        flag = "+" if mp >= 0 else "-"
        prof_months  += 1 if mp >= 0 else 0
        total_months += 1
        print(f"  {month:<10} {len(mt):>5} {mwr:>7} {flag}${abs(mp):<9.2f} ${running_bal:.2f}")

    pm_pct = round(prof_months / total_months * 100, 1) if total_months > 0 else 0.0
    print(f"\n  Profitable months : {prof_months}/{total_months}  ({pm_pct}%)")
    print(f"{SEP2}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=["GOLD", "BTCUSD", "both"], default="both")
    args = parser.parse_args()

    symbols = ["GOLD", "BTCUSD"] if args.symbol == "both" else [args.symbol]

    data_dir     = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(data_dir, exist_ok=True)
    summary_path = os.path.join(data_dir, "backtest_h_xm_summary.txt")

    all_output = []

    for symbol in symbols:
        sym_cfg = SYMBOL_CONFIGS[symbol]

        print(f"\n{'='*68}")
        print(f"  STRATEGY H XM BACKTEST  --  {symbol}")
        print(f"  {sym_cfg['label']}")
        print(f"{'='*68}")
        print(f"  Fetching M15 {symbol} from XM terminal...")

        try:
            bars = fetch_bars(symbol)
        except RuntimeError as e:
            print(f"  ERROR: {e}")
            continue

        print(f"\n  Running simulation (pip={sym_cfg['pip_size']} val=${sym_cfg['pip_val']}/lot)...")
        closed, final_bal, max_dd = run_simulation(bars, sym_cfg)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_stats(closed, final_bal, max_dd, sym_cfg["init_bal"], sym_cfg["label"])
        out = buf.getvalue()
        print(out)
        all_output.append(out)

        # Save trade CSV
        if closed:
            csv_path = os.path.join(data_dir, f"backtest_h_xm_{symbol}_trades.csv")
            all_keys = list(closed[0].keys())
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                w.writeheader()
                w.writerows(closed)
            print(f"  Trade log : {csv_path}")

    # Save combined summary
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(all_output))
    print(f"\n  Summary   : {summary_path}")
    print(f"  Done.\n")

    # Shutdown MT5 after all backtests
    mt5.shutdown()
