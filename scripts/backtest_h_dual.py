#!/usr/bin/env python3
"""
scripts/backtest_h_dual.py
Strategy H backtest for BOTH GOLD and BTCUSD with maximum available data.
Uses XM credentials, correct per-instrument params matching run_live_h_xm.py.

Usage:
    python scripts/backtest_h_dual.py
"""
import sys, os, csv, io, contextlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math, numpy as np, pandas as pd
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import MetaTrader5 as mt5
import config_xm as cfg

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_BULL, H4_BEAR, H4_NONE, BIAS_NEUTRAL,
)
from src.strategy_h.patterns import (
    detect_pattern_a, detect_pattern_b, detect_pattern_c, pattern_priority,
)
from src.strategy_h.exits import (
    compute_sl, check_sl_hit, check_tp1_hit,
    check_time_stop, update_trail_sl,
    check_regime_exit, sl_distance_valid,
)
from src.strategy_h.sizing import compute_lot

FLOAT_EPS = 1e-9

# ── Instrument configs (mirror run_live_h_xm.py exactly) ──────────────────────
INSTRUMENTS = {
    "GOLD": {
        "symbol":            "GOLD",
        "pip_size":          0.1,
        "pip_val":           10.0,
        "slippage":          0.05,
        "spread_cost":       0.15,
        "session_start":     6,    # 06-19 UTC London/NY sessions
        "session_end":       19,
        "weekdays_only":     True,
        "ctx_hour":          7,    # daily context updates at 07:00 UTC
        "risk_pct":          0.0075,
        "risk_pct_reduced":  0.004,
        "daily_loss_limit":  0.015,
        "consec_reduce":     3,
        "consec_halt":       5,
        "tp1_rr":            1.2,
        "pattern_b_only":    True,   # skip A and C for GOLD
        "regime_dir_filter": True,
        "halt_reset_zero":   True,
        "atr_low_factor":    0.5,
        "be_buffer_atr":     0.0,
        "atr_p":             14,
        "atr_ap":            20,
        "warmup":            250,
        "init_bal":          200.0,
        "pattern_a_expire":  4,
        "pattern_bc_expire": 1,
    },
    "BTCUSD": {
        "symbol":            "BTCUSD",
        "pip_size":          1.0,
        "pip_val":           1.0,
        "slippage":          0.50,
        "spread_cost":       50.0,
        "session_start":     0,    # 24/7
        "session_end":       24,
        "weekdays_only":     False,
        "ctx_hour":          0,    # daily context updates at 00:00 UTC
        "risk_pct":          0.01,
        "risk_pct_reduced":  0.005,
        "daily_loss_limit":  0.02,
        "consec_reduce":     3,
        "consec_halt":       6,
        "tp1_rr":            1.0,
        "pattern_b_only":    False,
        "regime_dir_filter": False,
        "halt_reset_zero":   False,
        "atr_low_factor":    0.4,
        "be_buffer_atr":     0.25,
        "atr_p":             14,
        "atr_ap":            20,
        "warmup":            250,
        "init_bal":          200.0,
        "pattern_a_expire":  4,
        "pattern_bc_expire": 1,
    },
}


# ── ATR helper ────────────────────────────────────────────────────────────────
def _atr(df: pd.DataFrame, p: int = 14) -> float:
    if len(df) < 2:
        return 1.0
    tr_list = []
    closes  = df["close"].values
    highs   = df["high"].values
    lows    = df["low"].values
    for k in range(1, len(df)):
        tr = max(highs[k] - lows[k],
                 abs(highs[k] - closes[k - 1]),
                 abs(lows[k]  - closes[k - 1]))
        tr_list.append(tr)
    if len(tr_list) == 0:
        return 1.0
    window = tr_list[-p:]
    return float(np.mean(window)) if window else 1.0


# ── Data fetch ────────────────────────────────────────────────────────────────
_mt5_connected = False

def ensure_connected():
    global _mt5_connected
    if _mt5_connected:
        return
    path = r"C:\Program Files\XM Global MT5\terminal64.exe"
    ok = mt5.initialize(path=path, login=cfg.LOGIN,
                        password=cfg.PASSWORD, server=cfg.SERVER)
    if not ok:
        # Try without path (if MT5 already running)
        ok = mt5.initialize(login=cfg.LOGIN,
                            password=cfg.PASSWORD, server=cfg.SERVER)
    if not ok:
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    info = mt5.account_info()
    print(f"  Connected: login={info.login}  balance=${info.balance:.2f}  server={cfg.SERVER}")
    _mt5_connected = True


def fetch_max(symbol: str) -> pd.DataFrame:
    """Fetch ALL available M15 bars from MT5 for this symbol."""
    ensure_connected()
    mt5.symbol_select(symbol, True)

    # Try a 10-year range first; MT5 returns whatever it has
    end_dt   = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=10 * 366)
    rates    = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M15, start_dt, end_dt)

    if rates is None or len(rates) < 500:
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 99999)

    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No M15 data returned for {symbol}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = df[["open", "high", "low", "close", "volume"]].copy()

    print(f"  {symbol}: {len(df):,} M15 bars  |  "
          f"{df.index[0].date()} → {df.index[-1].date()}")
    return df


# ── Simulation ─────────────────────────────────────────────────────────────────
def run_simulation(bars: pd.DataFrame, p: dict):
    """
    Full Strategy H simulation for one instrument.
    All parameters come from the `p` (cfg) dict — no module globals.
    """
    PIP_SIZE     = p["pip_size"]
    PIP_VAL      = p["pip_val"]
    SLIPPAGE     = p["slippage"]
    SPREAD_COST  = p["spread_cost"]
    SES_START    = p["session_start"]
    SES_END      = p["session_end"]
    WD_ONLY      = p["weekdays_only"]
    CTX_HOUR     = p["ctx_hour"]
    RISK_PCT     = p["risk_pct"]
    RISK_REDUCED = p["risk_pct_reduced"]
    LOSS_LIMIT   = p["daily_loss_limit"]
    C_REDUCE     = p["consec_reduce"]
    C_HALT       = p["consec_halt"]
    TP1_RR       = p["tp1_rr"]
    B_ONLY       = p["pattern_b_only"]
    DIR_FILTER   = p["regime_dir_filter"]
    HALT_ZERO    = p["halt_reset_zero"]
    ATR_LOW      = p["atr_low_factor"]
    BE_BUF       = p["be_buffer_atr"]
    ATR_P        = p["atr_p"]
    ATR_AP       = p["atr_ap"]
    WARMUP       = p["warmup"]
    INIT_BAL     = p["init_bal"]
    A_EXPIRE     = p["pattern_a_expire"]
    BC_EXPIRE    = p["pattern_bc_expire"]

    def _pnl(direction, entry, exit_p, lot):
        pts = exit_p - entry if direction == "long" else entry - exit_p
        return round(pts / PIP_SIZE * PIP_VAL * lot, 4)

    def _pnl_r(direction, entry, sl, exit_p):
        risk = abs(entry - sl)
        gain = exit_p - entry if direction == "long" else entry - exit_p
        return round(gain / risk, 3) if risk > FLOAT_EPS else 0.0

    def _tp1(entry, sl, direction):
        d = abs(entry - sl)
        return round(entry + d * TP1_RR if direction == "long" else entry - d * TP1_RR, 5)

    n        = len(bars)
    balance  = INIT_BAL
    peak_bal = INIT_BAL
    max_dd   = 0.0

    position  = None
    pending   = None
    closed    = []
    daily_pnl = 0.0
    daily_date = None
    consec_losses      = 0
    halt_cooldown_bar  = 0

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

        hour  = t.hour
        date  = t.date()
        wday  = t.weekday()   # 0=Mon … 6=Sun
        o_    = float(bar["open"])
        h_    = float(bar["high"])
        l_    = float(bar["low"])
        c_    = float(bar["close"])

        # ATR
        atr_win = bars.iloc[max(0, i - ATR_P - 5): i]
        atr     = _atr(atr_win, ATR_P)
        atr_avg = _atr(atr_win, min(ATR_AP, len(atr_win)))

        # Reset daily P&L
        if date != daily_date:
            daily_date = date
            daily_pnl  = 0.0

        # Build Asian range (22:00–06:59 UTC)
        if hour >= 22 or hour < 7:
            ab_h = max(ab_h, h_)
            ab_l = min(ab_l, l_)

        # Daily context update at CTX_HOUR UTC
        if hour == CTX_HOUR and date != last_ctx_date:
            last_ctx_date = date

            if ab_h > ab_l and ab_h != -1e18:
                asian_h = ab_h
                asian_l = ab_l
            ab_h = -1e18;  ab_l = 1e18

            d1_before = d1_all[d1_all.index.date < date]
            if len(d1_before) >= 2:
                pdh  = float(d1_before.iloc[-1]["high"])
                pdl  = float(d1_before.iloc[-1]["low"])
                pdc  = float(d1_before.iloc[-1]["close"])
                d1r  = d1_before.iloc[-min(20, len(d1_before)):]
                d1rh = float(d1r["high"].max())
                d1rl = float(d1r["low"].min())

            if len(d1_before) >= 12:
                d1_regime = compute_d1_regime(d1_before)

            h4_cutoff = pd.Timestamp(year=date.year, month=date.month,
                                     day=date.day, hour=CTX_HOUR, tzinfo=timezone.utc)
            h4_before = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4_cutoff]
            if len(h4_before) >= 12:
                h4_dir = compute_h4_direction(h4_before)

            if d1rh is not None and d1rl is not None:
                daily_bias, _ = compute_daily_bias(
                    d1_regime, h4_dir, c_, d1rh, d1rl, pdh, pdl, pdc,
                )

        # ── Manage existing position ──────────────────────────────────────────
        if position is not None:
            pos       = position
            direction = pos["direction"]
            bars_held = i - pos["open_bar"]

            sl_hit  = check_sl_hit(l_, h_, pos["current_sl"], direction)
            tp1_hit = (not pos["tp1_reached"] and
                       check_tp1_hit(l_, h_, pos["tp1"], direction))

            if sl_hit:
                fill      = (pos["current_sl"] - SLIPPAGE if direction == "long"
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
                    "exit_price":    fill,
                    "exit_reason":   "SL_HIT",
                    "pnl":           pnl,
                    "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
                    "bars_held":     bars_held,
                    "result":        result,
                    "balance_after": balance,
                })
                position = None;  pending = None
                continue

            if tp1_hit:
                half    = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(direction, pos["entry_price"], pos["tp1"], half)
                pos["tp1_reached"] = True
                pos["tp1_pnl"]     = tp1_pnl
                pos["half_lot"]    = half
                pos["best_price"]  = pos["tp1"]   # start tracking from TP1
                be_level = (pos["entry_price"] - BE_BUF * pos["atr_at_entry"]
                            if direction == "long"
                            else pos["entry_price"] + BE_BUF * pos["atr_at_entry"])
                pos["current_sl"] = (max(pos["current_sl"], be_level)
                                     if direction == "long"
                                     else min(pos["current_sl"], be_level))
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                new_trail = update_trail_sl(pos, m15_w, atr)
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail
                continue

            if pos["tp1_reached"]:
                # Update best price seen (high for long, low for short)
                cur_best = h_ if direction == "long" else l_
                pos["best_price"] = (max(pos.get("best_price", pos["entry_price"]), cur_best)
                                     if direction == "long"
                                     else min(pos.get("best_price", pos["entry_price"]), cur_best))
                m15_w = bars.iloc[max(0, i - 30): i + 1]
                new_trail = update_trail_sl(pos, m15_w, atr)
                if new_trail != pos["current_sl"]:
                    pos["current_sl"] = new_trail

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
            pnd       = pending
            direction = pnd["direction"]
            if i > pnd["expires_bar"]:
                pending = None
            else:
                filled     = False
                fill_price = None
                if pnd["order_type"] == "market":
                    filled     = True
                    fill_price = o_ + SLIPPAGE if direction == "long" else o_ - SLIPPAGE
                elif pnd["order_type"] == "limit":
                    ep = pnd["entry_price"]
                    if direction == "long" and l_ <= ep:
                        filled = True;  fill_price = ep + SLIPPAGE
                    elif direction == "short" and h_ >= ep:
                        filled = True;  fill_price = ep - SLIPPAGE

                if filled and fill_price is not None:
                    fill_price += SPREAD_COST / 2 if direction == "long" else -SPREAD_COST / 2
                    fill_price  = round(fill_price, 5)
                    sl          = compute_sl(fill_price, direction, pnd["atr"],
                                            pnd.get("sweep_extreme"))
                    tp1         = _tp1(fill_price, sl, direction)
                    if not sl_distance_valid(fill_price, sl, pnd["atr"]):
                        pending = None
                    else:
                        position = {
                            "direction":      direction,
                            "entry_price":    fill_price,
                            "sl":             sl,
                            "tp1":            tp1,
                            "current_sl":     sl,
                            "lot":            pnd["lot"],
                            "open_bar":       i,
                            "tp1_reached":    False,
                            "tp1_pnl":        0.0,
                            "half_lot":       max(0.01, round(pnd["lot"] * 0.5, 2)),
                            "pattern":        pnd["pattern"],
                            "atr_at_entry":   pnd["atr"],
                            "entry_time":     str(t),
                            "d1_regime":      d1_regime,
                            "h4_dir":         h4_dir,
                            "daily_bias":     daily_bias,
                            "session":        pnd.get("session", "?"),
                            "balance_before": balance,
                            "sl_distance":    abs(fill_price - sl),
                        }
                        pending = None

        # ── Detect new patterns ───────────────────────────────────────────────
        if position is not None or pending is not None:
            continue

        # Weekday filter
        if WD_ONLY and wday >= 5:
            continue

        # Session filter
        if not (SES_START <= hour < SES_END):
            continue

        # Daily loss halt
        if daily_pnl < -(balance * LOSS_LIMIT):
            continue

        # Consecutive losses halt
        if consec_losses >= C_HALT:
            if halt_cooldown_bar == 0:
                halt_cooldown_bar = i + 96
            if i < halt_cooldown_bar:
                continue
            else:
                consec_losses     = 0 if HALT_ZERO else C_REDUCE
                halt_cooldown_bar = 0

        # Regime filter
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue

        # ATR environment filter
        if atr_avg > 0:
            if atr > 3.0 * atr_avg:
                continue
            if atr < ATR_LOW * atr_avg:
                continue

        risk_pct   = RISK_REDUCED if consec_losses >= C_REDUCE else RISK_PCT
        m15_window = bars.iloc[max(0, i - 60): i + 1]

        for direction in ("long", "short"):
            # Regime direction filter (GOLD only)
            if DIR_FILTER:
                if d1_regime == D1_BULL and direction != "long":
                    continue
                if d1_regime == D1_BEAR and direction != "short":
                    continue

            if not bias_allows_direction(daily_bias, direction):
                continue

            setups = []

            if not B_ONLY:
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

            test_entry = setup["entry_price"]
            sweep_ext  = setup.get("sweep_extreme")
            sl_test    = compute_sl(test_entry, direction, atr, sweep_ext)
            if not sl_distance_valid(test_entry, sl_test, atr):
                continue

            lot = compute_lot(balance, abs(test_entry - sl_test), risk_pct)

            if setup["type"] == "A":
                order_type  = "limit"
                expires_bar = i + A_EXPIRE
            else:
                order_type  = "market"
                expires_bar = i + BC_EXPIRE

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
            break   # one direction per bar

    # Force-close at final bar
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
            "exit_price":    fill,
            "exit_reason":   "FORCE_CLOSE",
            "pnl":           pnl,
            "pnl_r":         _pnl_r(direction, pos["entry_price"], pos["sl"], fill),
            "bars_held":     n - 1 - pos["open_bar"],
            "result":        "win" if pnl > 0 else "loss",
            "balance_after": balance,
        })

    return closed, balance, max_dd


# ── Stats printer ──────────────────────────────────────────────────────────────
def print_stats(instr_name: str, closed: list, final_bal: float,
                max_dd: float, init_bal: float, bars: pd.DataFrame):
    SEP  = "─" * 70
    SEP2 = "═" * 70

    period = f"{bars.index[0].date()} → {bars.index[-1].date()}"

    print(f"\n{SEP2}")
    print(f"  STRATEGY H — {instr_name}   |   {period}")
    print(f"{SEP2}")

    if not closed:
        print("  No trades generated.")
        print(f"{SEP2}\n")
        return

    total  = len(closed)
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else 99.0
    net    = round(gw - gl, 2)
    ret    = round((final_bal - init_bal) / init_bal * 100, 1)
    wr     = round(len(wins) / total * 100, 1)
    avg_w  = round(gw / len(wins), 2)   if wins   else 0.0
    avg_l  = round(gl / len(losses), 2) if losses else 0.0
    exp_r  = round(sum(t.get("pnl_r", 0) for t in closed) / total, 3)

    sl_hits   = sum(1 for t in closed if t.get("exit_reason") == "SL_HIT")
    t_stops   = sum(1 for t in closed if t.get("exit_reason") == "TIME_STOP")
    r_exits   = sum(1 for t in closed if t.get("exit_reason") == "REGIME_EXIT")
    tp1_hits  = sum(1 for t in closed if t.get("tp1_reached"))

    print(f"  Initial balance  : ${init_bal:.2f}")
    print(f"  Final balance    : ${final_bal:.2f}  ({'+' if ret >= 0 else ''}{ret}%)")
    print(f"  Net P&L          : ${net:.2f}")
    print(f"  Max Drawdown     : {max_dd:.1f}%")
    print(f"{SEP}")
    print(f"  Total trades     : {total}")
    print(f"  Win Rate         : {wr}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor    : {pf}")
    print(f"  Avg Win / Loss   : ${avg_w:.2f} / ${avg_l:.2f}")
    print(f"  Expectancy       : {exp_r:+.3f}R per trade")
    print(f"  Gross Win / Loss : ${gw:.2f} / ${gl:.2f}")
    print(f"{SEP}")
    print(f"  Exit breakdown:")
    print(f"    SL Hit         : {sl_hits:>4}  ({round(sl_hits/total*100,1)}%)")
    print(f"    Time Stop      : {t_stops:>4}  ({round(t_stops/total*100,1)}%)")
    print(f"    Trail/Regime   : {r_exits:>4}  ({round(r_exits/total*100,1)}%)")
    print(f"  TP1 reached      : {tp1_hits:>4}  ({round(tp1_hits/total*100,1)}%)")

    # Per-pattern
    pat_map = defaultdict(list)
    for t in closed:
        pat_map[t.get("pattern", "?")].append(t)

    print(f"\n{SEP}")
    print(f"  PER-PATTERN BREAKDOWN")
    print(f"  {'Pattern':<10} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*50}")
    for ptype in sorted(pat_map.keys()):
        pt  = pat_map[ptype]
        pw  = [t for t in pt if t["pnl"] > 0]
        pl_ = [t for t in pt if t["pnl"] <= 0]
        pg  = sum(t["pnl"] for t in pw)
        plo = abs(sum(t["pnl"] for t in pl_))
        ppf = round(pg / plo, 2) if plo > 0 else 99.0
        pwr = round(len(pw) / len(pt) * 100, 1)
        pp  = sum(t["pnl"] for t in pt)
        print(f"  {'Pattern '+ptype:<10} {len(pt):>7} {pwr:>7} {ppf:>7} {f'${pp:.2f}':>11}")

    # Per-direction
    dir_map = defaultdict(list)
    for t in closed:
        dir_map[t.get("direction", "?")].append(t)

    print(f"\n{SEP}")
    print(f"  PER-DIRECTION BREAKDOWN")
    print(f"  {'Dir':<10} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*50}")
    for dr in ("long", "short"):
        dt = dir_map.get(dr, [])
        if not dt:
            continue
        dw  = [t for t in dt if t["pnl"] > 0]
        dl_ = [t for t in dt if t["pnl"] <= 0]
        dg  = sum(t["pnl"] for t in dw)
        dlo = abs(sum(t["pnl"] for t in dl_))
        dpf = round(dg / dlo, 2) if dlo > 0 else 99.0
        dwr = round(len(dw) / len(dt) * 100, 1)
        dp  = sum(t["pnl"] for t in dt)
        print(f"  {dr.upper():<10} {len(dt):>7} {dwr:>7} {dpf:>7} {f'${dp:.2f}':>11}")

    # Per-regime
    reg_map = defaultdict(list)
    for t in closed:
        reg_map[t.get("d1_regime", "?")].append(t)

    print(f"\n{SEP}")
    print(f"  PER D1-REGIME BREAKDOWN")
    print(f"  {'Regime':<12} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Net P&L':>11}")
    print(f"  {'─'*50}")
    for rg in ("bull", "bear", "range", "compress", "chop"):
        rt = reg_map.get(rg, [])
        if not rt:
            continue
        rw  = [t for t in rt if t["pnl"] > 0]
        rl_ = [t for t in rt if t["pnl"] <= 0]
        rg_ = sum(t["pnl"] for t in rw)
        rlo = abs(sum(t["pnl"] for t in rl_))
        rpf = round(rg_ / rlo, 2) if rlo > 0 else 99.0
        rwr = round(len(rw) / len(rt) * 100, 1)
        rp  = sum(t["pnl"] for t in rt)
        print(f"  {rg.upper():<12} {len(rt):>7} {rwr:>7} {rpf:>7} {f'${rp:.2f}':>11}")

    # Monthly
    monthly = defaultdict(list)
    for t in closed:
        et = str(t.get("entry_time", ""))[:7]
        if et:
            monthly[et].append(t)

    print(f"\n{SEP}")
    print(f"  MONTHLY BREAKDOWN")
    print(f"  {'Month':<10} {'Trades':>7} {'WR%':>7} {'P&L':>11} {'Balance':>10}")
    print(f"  {'─'*52}")
    running_bal = init_bal
    prof_months = total_months = 0
    for month in sorted(monthly.keys()):
        mt_  = monthly[month]
        mw   = sum(1 for t in mt_ if t["pnl"] > 0)
        mp   = sum(t["pnl"] for t in mt_)
        mwr  = round(mw / len(mt_) * 100, 1)
        running_bal += mp
        flag = "▲" if mp >= 0 else "▼"
        prof_months  += (1 if mp >= 0 else 0)
        total_months += 1
        print(f"  {month:<10} {len(mt_):>7} {mwr:>7} {flag}${abs(mp):<9.2f} ${running_bal:.2f}")

    pm_pct = round(prof_months / total_months * 100, 1) if total_months > 0 else 0.0
    print(f"\n  Profitable months : {prof_months}/{total_months}  ({pm_pct}%)")
    print(f"{SEP2}\n")


# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═" * 70)
    print("  STRATEGY H — DUAL BACKTEST: GOLD + BTCUSD (Max Available Data)")
    print("  Using XM demo account credentials")
    print("═" * 70 + "\n")

    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(data_dir, exist_ok=True)

    results = {}

    for instr_name, instr_cfg in INSTRUMENTS.items():
        symbol = instr_cfg["symbol"]
        print(f"Fetching {instr_name} ({symbol})...")
        bars = fetch_max(symbol)

        print(f"Running {instr_name} simulation...")
        closed, final_bal, max_dd = run_simulation(bars, instr_cfg)

        results[instr_name] = (closed, final_bal, max_dd, bars)

        # Save trade log CSV
        if closed:
            out_path = os.path.join(data_dir, f"backtest_dual_{instr_name.lower()}_trades.csv")
            keys = list(closed[0].keys())
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                w.writerows(closed)
            print(f"  Trade log → {out_path}")

        print()

    # Shut down MT5 connection
    mt5.shutdown()

    # Print stats for each instrument
    for instr_name, (closed, final_bal, max_dd, bars) in results.items():
        print_stats(instr_name, closed, final_bal, max_dd,
                    INSTRUMENTS[instr_name]["init_bal"], bars)

    # Combined summary
    all_closed = []
    for closed, final_bal, max_dd, bars in results.values():
        all_closed.extend(closed)

    if len(results) == 2:
        gold_closed, gold_bal, gold_dd, _ = results["GOLD"]
        btc_closed,  btc_bal,  btc_dd,  _ = results["BTCUSD"]
        combined_bal = gold_bal + btc_bal
        combined_start = 400.0
        combined_ret = round((combined_bal - combined_start) / combined_start * 100, 1)

        print("═" * 70)
        print("  COMBINED PORTFOLIO SUMMARY")
        print("═" * 70)
        print(f"  GOLD start/end  : $200.00 → ${gold_bal:.2f}")
        print(f"  BTC  start/end  : $200.00 → ${btc_bal:.2f}")
        print(f"  Portfolio total : $400.00 → ${combined_bal:.2f}  ({'+' if combined_ret>=0 else ''}{combined_ret}%)")
        print(f"  GOLD max DD     : {gold_dd:.1f}%")
        print(f"  BTC  max DD     : {btc_dd:.1f}%")
        all_t = gold_closed + btc_closed
        if all_t:
            all_w = [t for t in all_t if t["pnl"] > 0]
            all_wr = round(len(all_w) / len(all_t) * 100, 1)
            all_gw = sum(t["pnl"] for t in all_w)
            all_gl = abs(sum(t["pnl"] for t in all_t if t["pnl"] <= 0))
            all_pf = round(all_gw / all_gl, 2) if all_gl > 0 else 99.0
            print(f"  Total trades    : {len(all_t)}  (GOLD={len(gold_closed)}, BTC={len(btc_closed)})")
            print(f"  Combined WR     : {all_wr}%")
            print(f"  Combined PF     : {all_pf}")
        print("═" * 70 + "\n")

    # Save summary to file
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        for instr_name, (closed, final_bal, max_dd, bars) in results.items():
            print_stats(instr_name, closed, final_bal, max_dd,
                        INSTRUMENTS[instr_name]["init_bal"], bars)
    summary_path = os.path.join(data_dir, "backtest_dual_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    print(f"Summary saved → {summary_path}")
