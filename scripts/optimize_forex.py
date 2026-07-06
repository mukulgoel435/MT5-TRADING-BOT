#!/usr/bin/env python3
"""
scripts/optimize_forex.py
=========================
Grid-search optimal Strategy H parameters for EURUSD, GBPUSD, USDJPY.

Uses vectorised numpy signal detection (not the production pattern functions)
so each simulation runs in ~1-2s instead of 20s.

Parameters tested per instrument (54 combinations):
  session_mode : kz_only | kz_tokyo | london_ny | full
  bias_strict  : True | False   (skip NEUTRAL bias if True)
  reject_wick  : 0.20 | 0.25 | 0.30
  tp_rr        : 2.0 | 2.5 | 3.0

Usage:
    py -3 scripts/optimize_forex.py
"""

import sys, os, itertools, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import MetaTrader5 as mt5
import config

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_CHOP, D1_COMPRESS,
    H4_NONE, BIAS_NEUTRAL,
)
from src.strategy_h.sizing import compute_lot


# ── Instruments ───────────────────────────────────────────────────────────────
FOREX_PAIRS = {
    "EURUSD": {
        "pip_size":      0.0001,
        "pip_val":       10.0,
        "spread_pts":    0.00012,
        "slippage":      0.00003,
        "label":         "Euro / Dollar",
        "session_opts":  ["kz_only", "london_ny", "full"],
    },
    "GBPUSD": {
        "pip_size":      0.0001,
        "pip_val":       10.0,
        "spread_pts":    0.00018,
        "slippage":      0.00003,
        "label":         "Pound / Dollar",
        "session_opts":  ["kz_only", "london_ny", "full"],
    },
    "USDJPY": {
        "pip_size":      0.01,
        "pip_val":       9.0,
        "spread_pts":    0.015,
        "slippage":      0.005,
        "label":         "Dollar / Yen",
        "session_opts":  ["kz_tokyo", "kz_only", "full"],
    },
}

# Session scan hours: list of (start_h, end_h) pairs
SESSION_HOURS = {
    "kz_only":   [(7, 10), (13, 16)],           # London + NY kill zones
    "kz_tokyo":  [(1,  4), (7, 10), (13, 16)],  # Tokyo + London + NY kill zones
    "london_ny": [(7, 21)],                      # full European + US day
    "full":      [(0, 24)],                      # 24/7
}

# ── Parameter grid (54 combos per pair) ──────────────────────────────────────
GRID_BIAS_STRICT = [True, False]
GRID_REJECT_WICK = [0.20, 0.25, 0.30]
GRID_TP_RR       = [2.0,  2.5,  3.0]

# ── Shared sim constants ──────────────────────────────────────────────────────
INIT_BAL         = 1000.0
RISK_PCT         = 0.01
RISK_PCT_REDUCED = 0.005
DAILY_LOSS_LIMIT = 0.02
CONSEC_REDUCE    = 3
CONSEC_HALT      = 6
WARMUP           = 250
TIME_STOP_BARS   = 64    # 16 hours on M15
BOS_WINDOW       = 20    # bars to look back for swing high/low
EQL_WINDOW       = 12    # bars to look back for equal levels
EQL_TOL_ATR      = 0.80  # equal level clustering tolerance as ATR fraction
SWEEP_MIN_ATR    = 0.30  # minimum sweep extension
FLOAT_EPS        = 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _in_session(hour, mode):
    for (s, e) in SESSION_HOURS[mode]:
        if s <= hour < e:
            return True
    return False


def _pnl(direction, entry, exit_p, lot, pip_size, pip_val):
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / pip_size * pip_val * lot, 4)


def _compute_sl(entry, direction, atr, sweep_extreme=None):
    if direction == "long":
        ref = min(sweep_extreme, entry - atr * 0.5) if sweep_extreme else entry - atr * 0.5
        return round(ref - atr * 0.3, 8)
    else:
        ref = max(sweep_extreme, entry + atr * 0.5) if sweep_extreme else entry + atr * 0.5
        return round(ref + atr * 0.3, 8)


def _compute_tp1(entry, sl, direction, rr):
    dist = abs(entry - sl)
    return (entry + dist * rr) if direction == "long" else (entry - dist * rr)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_m15(symbol, years=2):
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
    return df[df.index >= cutoff].copy()


# ── Daily context cache (vectorised, fast) ────────────────────────────────────

def precompute_daily_context(bars):
    """
    Vectorised: computes D1 regime, H4 direction, daily bias, Asian range
    for every calendar date in bars. Returns dict keyed by date.
    Runs in ~0.5s on 50k bars.
    """
    OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4_all = bars.resample("4h").agg(OHLCV_AGG).dropna()

    # Asian range vectorised
    hours = bars.index.hour.values
    dates_norm = bars.index.normalize()
    session_dates = np.where(
        hours >= 22,
        dates_norm + pd.Timedelta(days=1),
        dates_norm,
    )
    bars2 = bars.copy()
    bars2["_sd"]  = session_dates
    bars2["_hr"]  = hours
    asian_mask = (hours >= 22) | (hours < 7)
    if asian_mask.any():
        ab = bars2[asian_mask].groupby("_sd").agg({"high": "max", "low": "min"})
        asian_cache = {
            pd.Timestamp(k).date(): (float(h), float(l))
            for k, h, l in zip(ab.index, ab["high"].values, ab["low"].values)
        }
    else:
        asian_cache = {}

    open7 = (bars2[bars2["_hr"] == 7]
             .groupby(bars2[bars2["_hr"] == 7].index.date)["close"]
             .first())

    d1_dates = np.array([t.date() for t in d1_all.index])
    h4_idx   = h4_all.index

    ctx_cache = {}
    for date in sorted(set(bars.index.date)):
        n_d1 = int((d1_dates < date).sum())
        if n_d1 < 2:
            continue
        d1_before = d1_all.iloc[:n_d1]
        pdh  = float(d1_before.iloc[-1]["high"])
        pdl  = float(d1_before.iloc[-1]["low"])
        pdc  = float(d1_before.iloc[-1]["close"])
        rec  = d1_before.iloc[-min(20, n_d1):]
        d1rh = float(rec["high"].max())
        d1rl = float(rec["low"].min())

        d1_regime = "range"
        if n_d1 >= 12:
            d1_regime = compute_d1_regime(d1_before)

        h4_cut = pd.Timestamp(year=date.year, month=date.month,
                               day=date.day, hour=7, tzinfo=timezone.utc)
        h4_msk = h4_idx + pd.Timedelta(hours=4) <= h4_cut
        h4_dir = H4_NONE
        if h4_msk.sum() >= 12:
            h4_dir = compute_h4_direction(h4_all[h4_msk])

        price = float(open7.get(date, pdc))
        daily_bias, _ = compute_daily_bias(
            d1_regime, h4_dir, price, d1rh, d1rl, pdh, pdl, pdc
        )
        asian = asian_cache.get(date, (None, None))
        ctx_cache[date] = {
            "d1_regime": d1_regime, "h4_dir": h4_dir, "daily_bias": daily_bias,
            "d1rh": d1rh, "d1rl": d1rl, "pdh": pdh, "pdl": pdl, "pdc": pdc,
            "asian_h": asian[0], "asian_l": asian[1],
        }
    return ctx_cache


# ── Fast signal arrays (vectorised) ──────────────────────────────────────────

def build_signal_arrays(bars):
    """
    Pre-compute all pattern signals as numpy arrays.
    Uses vectorised operations — replaces the slow per-bar pattern calls.

    Returns dict of arrays (length = len(bars)):
      atr, body_frac, wick_long, wick_short,
      bos_long_lvl, bos_short_lvl,   (NaN if no BOS)
      eql_sweep_ext, eqh_sweep_ext,  (in ATR units; NaN if no sweep)
      hour
    """
    h = bars["high"].values.astype(np.float64)
    l = bars["low"].values.astype(np.float64)
    c = bars["close"].values.astype(np.float64)
    o = bars["open"].values.astype(np.float64)
    n = len(bars)

    # ── ATR (14-period) ───────────────────────────────────────────────────
    prev_c = np.empty(n); prev_c[0] = c[0]; prev_c[1:] = c[:-1]
    tr  = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = np.full(n, np.nan)
    for i in range(14, n):
        atr[i] = tr[i-14:i].mean()

    body  = np.abs(c - o)
    rng   = np.maximum(h - l, FLOAT_EPS)

    # ── Rejection wicks ───────────────────────────────────────────────────
    lo_c = np.minimum(o, c)   # lower of open/close
    hi_c = np.maximum(o, c)   # upper of open/close
    lower_wick = lo_c - l     # wick below body (for long rejection)
    upper_wick = h - hi_c     # wick above body (for short rejection)
    wick_long  = lower_wick / rng
    wick_short = upper_wick / rng

    # ── BOS level (Pattern B signal) ─────────────────────────────────────
    # Bull BOS bar i: close[i] crosses 20-bar rolling high with body >= 0.4×ATR
    # We record the BOS level and carry it forward for retest checking.
    bos_long_lvl  = np.full(n, np.nan)   # carried-forward BOS level (long)
    bos_short_lvl = np.full(n, np.nan)   # carried-forward BOS level (short)

    carry_l = np.nan   # most recent bull BOS level
    carry_s = np.nan   # most recent bear BOS level
    carry_l_bar = -999
    carry_s_bar = -999
    MAX_CARRY  = 30    # BOS level expires after 30 bars

    for i in range(BOS_WINDOW + 1, n):
        if np.isnan(atr[i]):
            continue
        w_h = h[i-BOS_WINDOW:i]
        w_l = l[i-BOS_WINDOW:i]
        swing_h = w_h.max()
        swing_l = w_l.min()

        # New bull BOS
        if c[i] > swing_h and body[i] >= 0.4 * atr[i]:
            carry_l     = swing_h
            carry_l_bar = i
        # New bear BOS
        if c[i] < swing_l and body[i] >= 0.4 * atr[i]:
            carry_s     = swing_l
            carry_s_bar = i

        # Expire old BOS levels
        if i - carry_l_bar > MAX_CARRY:
            carry_l = np.nan
        if i - carry_s_bar > MAX_CARRY:
            carry_s = np.nan

        bos_long_lvl[i]  = carry_l
        bos_short_lvl[i] = carry_s

    # ── Equal level sweep (Pattern C signal) ─────────────────────────────
    # For LONG: equal lows cluster in last EQL_WINDOW bars
    #   → bar swept below cluster (l < cluster_level)
    #   → bar closed back above cluster
    # Simplified: cluster_level = min of rolling-window lows
    #   Sweep if: l < cluster AND c > cluster AND (cluster - l) >= SWEEP_MIN_ATR×ATR

    eql_sweep_ext = np.full(n, np.nan)   # sweep extension / ATR (long)
    eqh_sweep_ext = np.full(n, np.nan)   # sweep extension / ATR (short)

    for i in range(EQL_WINDOW + 2, n):
        if np.isnan(atr[i]):
            continue
        w = slice(i - EQL_WINDOW, i - 1)   # exclude signal bar itself
        cluster_l = l[w].min()   # equal low cluster approximation
        cluster_h = h[w].max()   # equal high cluster approximation

        # Only treat as equal levels if range of recent lows/highs is tight
        low_std  = l[w].std()
        high_std = h[w].std()
        if low_std < EQL_TOL_ATR * atr[i]:
            # Long: wick swept below cluster, closed back above
            ext = cluster_l - l[i]
            if ext >= SWEEP_MIN_ATR * atr[i] and c[i] > cluster_l:
                eql_sweep_ext[i] = ext / atr[i]

        if high_std < EQL_TOL_ATR * atr[i]:
            # Short: wick swept above cluster, closed back below
            ext = h[i] - cluster_h
            if ext >= SWEEP_MIN_ATR * atr[i] and c[i] < cluster_h:
                eqh_sweep_ext[i] = ext / atr[i]

    hours = np.array(bars.index.hour)

    return {
        "atr":          atr,
        "body":         body,
        "rng":          rng,
        "wick_long":    wick_long,
        "wick_short":   wick_short,
        "bos_long_lvl": bos_long_lvl,
        "bos_short_lvl":bos_short_lvl,
        "eql_ext":      eql_sweep_ext,
        "eqh_ext":      eqh_sweep_ext,
        "hour":         hours,
        "open":         o,
        "high":         h,
        "low":          l,
        "close":        c,
        "dates":        np.array(bars.index.date),
    }


# ── Fast simulation (uses precomputed signal arrays) ─────────────────────────

def run_fast_sim(sig, inst, params, ctx_cache):
    """
    Fast simulation using precomputed signal arrays.
    Pattern detection is O(1) per bar via array lookup.
    """
    pip_size   = inst["pip_size"]
    pip_val    = inst["pip_val"]
    spread_pts = inst["spread_pts"]
    slippage   = inst["slippage"]
    session    = params["session_mode"]
    strict     = params["bias_strict"]
    wick_thr   = params["reject_wick"]
    tp_rr      = params["tp_rr"]

    n         = len(sig["close"])
    balance   = INIT_BAL
    peak_bal  = INIT_BAL
    max_dd    = 0.0
    position  = None
    pending   = None
    closed    = []
    daily_pnl = 0.0
    prev_date  = None
    consec_losses = 0
    halt_cooldown = 0

    for i in range(WARMUP, n):
        date  = sig["dates"][i]
        hour  = int(sig["hour"][i])
        atr   = sig["atr"][i]
        c_    = sig["close"][i]
        h_    = sig["high"][i]
        l_    = sig["low"][i]
        o_    = sig["open"][i]

        if atr == 0 or np.isnan(atr):
            continue

        if date != prev_date:
            prev_date = date
            daily_pnl = 0.0

        ctx = ctx_cache.get(date, {})
        d1_regime  = ctx.get("d1_regime",  "range")
        h4_dir     = ctx.get("h4_dir",     H4_NONE)
        daily_bias = ctx.get("daily_bias",  BIAS_NEUTRAL)
        asian_h    = ctx.get("asian_h")
        asian_l    = ctx.get("asian_l")

        # ── Position management ────────────────────────────────────────────
        if position is not None:
            pos       = position
            direction = pos["d"]
            bars_held = i - pos["bar"]

            # Update best price
            if direction == "long":
                pos["best"] = max(pos["best"], h_)
            else:
                pos["best"] = min(pos["best"], l_)

            # SL hit
            sl_hit = ((direction == "long" and l_ <= pos["sl"]) or
                      (direction == "short" and h_ >= pos["sl"]))
            if sl_hit:
                fill = (pos["sl"] - slippage if direction == "long"
                        else pos["sl"] + slippage)
                rem  = pos["half_lot"] if pos["tp1_done"] else pos["lot"]
                pnl  = _pnl(direction, pos["entry"], fill, rem, pip_size, pip_val)
                if pos["tp1_done"]:
                    pnl += pos["tp1_pnl"]
                balance   += pnl;  daily_pnl += pnl
                peak_bal   = max(peak_bal, balance)
                max_dd     = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                consec_losses = 0 if pnl > 0 else consec_losses + 1
                closed.append({"pnl": pnl, "d": direction, "pat": pos["pat"],
                                "exit": "SL", "bars": bars_held})
                position = None;  pending = None;  continue

            # TP1 hit
            tp1_hit = (not pos["tp1_done"] and
                       ((direction == "long"  and h_ >= pos["tp1"]) or
                        (direction == "short" and l_ <= pos["tp1"])))
            if tp1_hit:
                half    = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(direction, pos["entry"], pos["tp1"], half, pip_size, pip_val)
                pos["tp1_done"] = True
                pos["tp1_pnl"]  = tp1_pnl
                pos["half_lot"] = half
                pos["sl"]       = pos["entry"]   # breakeven
                continue

            # ATR trail after TP1
            if pos["tp1_done"]:
                if direction == "long":
                    trail = pos["best"] - 1.5 * atr
                    if trail > pos["sl"]:
                        pos["sl"] = trail
                else:
                    trail = pos["best"] + 1.5 * atr
                    if trail < pos["sl"]:
                        pos["sl"] = trail

            # Time stop
            if bars_held >= TIME_STOP_BARS:
                fill = c_ - slippage if direction == "long" else c_ + slippage
                rem  = pos["half_lot"] if pos["tp1_done"] else pos["lot"]
                pnl  = _pnl(direction, pos["entry"], fill, rem, pip_size, pip_val)
                if pos["tp1_done"]:
                    pnl += pos["tp1_pnl"]
                balance += pnl;  daily_pnl += pnl
                peak_bal = max(peak_bal, balance)
                max_dd   = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                consec_losses = 0 if pnl > 0 else consec_losses + 1
                closed.append({"pnl": pnl, "d": direction, "pat": pos["pat"],
                                "exit": "TIME", "bars": bars_held})
                position = None;  continue

        # ── Fill pending order ─────────────────────────────────────────────
        if pending is not None and position is None:
            p = pending
            if i > p["exp"]:
                pending = None
            else:
                fill_p = None
                if p["otype"] == "market":
                    fill_p = o_ + slippage if p["d"] == "long" else o_ - slippage
                elif p["otype"] == "limit":
                    if p["d"] == "long"  and l_ <= p["ep"]:
                        fill_p = p["ep"] + slippage
                    elif p["d"] == "short" and h_ >= p["ep"]:
                        fill_p = p["ep"] - slippage
                if fill_p is not None:
                    fill_p += spread_pts/2 if p["d"] == "long" else -spread_pts/2
                    sl      = _compute_sl(fill_p, p["d"], p["atr"], p.get("sw"))
                    tp1     = _compute_tp1(fill_p, sl, p["d"], tp_rr)
                    sl_dist = abs(fill_p - sl)
                    if sl_dist >= 0.3 * atr:   # valid SL distance
                        lot = compute_lot(balance, sl_dist,
                                         RISK_PCT_REDUCED if consec_losses >= CONSEC_REDUCE else RISK_PCT)
                        position = {
                            "d": p["d"], "entry": fill_p,
                            "sl": sl, "tp1": tp1,
                            "lot": lot, "bar": i,
                            "tp1_done": False, "tp1_pnl": 0.0,
                            "half_lot": max(0.01, round(lot*0.5, 2)),
                            "pat": p["pat"], "atr": p["atr"],
                            "best": fill_p,
                        }
                        pending = None

        # ── New signal detection (fast array lookups) ──────────────────────
        if position is not None or pending is not None:
            continue
        if not _in_session(hour, session):
            continue
        if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
            continue
        if consec_losses >= CONSEC_HALT:
            if halt_cooldown == 0:
                halt_cooldown = i + 96
            if i < halt_cooldown:
                continue
            else:
                consec_losses = CONSEC_REDUCE;  halt_cooldown = 0
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue

        # Pull precomputed values for bar i
        wl   = sig["wick_long"][i]
        ws   = sig["wick_short"][i]
        bl   = sig["bos_long_lvl"][i]   # most recent bull BOS level (or NaN)
        bs   = sig["bos_short_lvl"][i]  # most recent bear BOS level (or NaN)
        eql  = sig["eql_ext"][i]        # EQL sweep extension in ATR (or NaN)
        eqh  = sig["eqh_ext"][i]        # EQH sweep extension in ATR (or NaN)

        for direction in ("long", "short"):
            if not bias_allows_direction(daily_bias, direction):
                continue
            if strict and daily_bias == BIAS_NEUTRAL:
                continue

            pat    = None
            entry  = None
            sweep  = None
            otype  = "market"

            # Pattern C: equal level sweep (highest priority)
            if direction == "long" and not np.isnan(eql) and eql >= SWEEP_MIN_ATR:
                pat   = "C";  entry = c_;  sweep = l_;  otype = "market"
            elif direction == "short" and not np.isnan(eqh) and eqh >= SWEEP_MIN_ATR:
                pat   = "C";  entry = c_;  sweep = h_;  otype = "market"

            # Pattern B: BOS retest with rejection wick
            if pat is None:
                if direction == "long" and not np.isnan(bl):
                    dist_ok = abs(l_ - bl) <= 2.0 * atr
                    if dist_ok and wl >= wick_thr:
                        pat  = "B";  entry = c_;  sweep = l_;  otype = "market"
                elif direction == "short" and not np.isnan(bs):
                    dist_ok = abs(h_ - bs) <= 2.0 * atr
                    if dist_ok and ws >= wick_thr:
                        pat  = "B";  entry = c_;  sweep = h_;  otype = "market"

            if pat is None or entry is None:
                continue

            # Quick SL validity check
            sl_test = _compute_sl(entry, direction, atr, sweep)
            if abs(entry - sl_test) < 0.3 * atr:
                continue

            pending = {
                "d": direction, "ep": entry, "sw": sweep,
                "atr": atr, "pat": pat, "otype": otype,
                "exp": i + 1,
            }
            break   # take first valid signal per bar

    # Force-close end
    if position is not None:
        pos = position;  direction = pos["d"]
        fill = sig["close"][-1]
        rem  = pos["half_lot"] if pos["tp1_done"] else pos["lot"]
        pnl  = _pnl(direction, pos["entry"], fill, rem, pip_size, pip_val)
        if pos["tp1_done"]:
            pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({"pnl": pnl, "d": direction, "pat": pos["pat"], "exit": "END", "bars": 0})

    return closed, balance, max_dd


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(closed, final_bal, max_dd, n_bars):
    if not closed:
        return {"trades":0,"wr":0,"pf":0,"net":0,"ret":0,"max_dd":max_dd,"tpd":0}
    total = len(closed)
    wins  = [t for t in closed if t["pnl"] > 0]
    losses= [t for t in closed if t["pnl"] <= 0]
    gw    = sum(t["pnl"] for t in wins)
    gl    = abs(sum(t["pnl"] for t in losses))
    tdays = n_bars / (24 * 4)
    return {
        "trades": total,
        "wr":     round(len(wins)/total*100, 1),
        "pf":     round(gw/gl, 2) if gl > 0 else 9.99,
        "net":    round(gw - gl, 2),
        "ret":    round((final_bal - INIT_BAL)/INIT_BAL*100, 1),
        "max_dd": round(max_dd, 1),
        "tpd":    round(total/max(tdays,1), 2),
    }


def score_result(s):
    if s["trades"] < 50 or s["max_dd"] > 50:
        return -999.0
    return ((s["pf"] - 1.0) * 100 +
            (s["wr"]  - 40.0) * 2  -
            max(0.0, s["max_dd"] - 20.0) * 2 +
            math.log(max(s["trades"], 1)) * 3)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    SEP  = "-" * 80
    SEP2 = "=" * 80

    print(f"\n{SEP2}")
    print(f"  STRATEGY H -- FOREX PARAMETER OPTIMIZER  |  2-Year M15")
    print(f"  Balance: ${INIT_BAL:.0f}  |  Risk: {RISK_PCT*100:.0f}%  |  54 combos x 3 pairs")
    print(f"{SEP2}\n")

    if not mt5.initialize(login=config.LOGIN, password=config.PASSWORD,
                          server=config.SERVER):
        print(f"  MT5 init failed: {mt5.last_error()}")
        return

    best_per_inst = {}

    for symbol, inst in FOREX_PAIRS.items():
        print(f"\n{SEP2}")
        print(f"  {symbol} -- {inst['label']}")
        print(f"{SEP2}")

        try:
            bars = fetch_m15(symbol, years=2)
        except Exception as e:
            print(f"  ERROR fetching: {e}"); continue

        print(f"  {len(bars):,} M15 bars  ({bars.index[0].date()} to {bars.index[-1].date()})")

        print(f"  Building signal arrays...", flush=True, end="")
        t0 = time.time()
        sig = build_signal_arrays(bars)
        print(f" {time.time()-t0:.1f}s")

        print(f"  Building daily context...", flush=True, end="")
        t0 = time.time()
        ctx = precompute_daily_context(bars)
        print(f" {time.time()-t0:.1f}s  ({len(ctx)} dates)")

        sess_opts = inst["session_opts"]
        combos = list(itertools.product(
            sess_opts,
            GRID_BIAS_STRICT,
            GRID_REJECT_WICK,
            GRID_TP_RR,
        ))
        total = len(combos)
        print(f"\n  Running {total} combinations...\n")

        all_results = []
        t_all = time.time()

        for idx, (sess, strict, wick, tprr) in enumerate(combos, 1):
            params = {
                "session_mode": sess,
                "bias_strict":  strict,
                "reject_wick":  wick,
                "tp_rr":        tprr,
            }
            try:
                closed, final_bal, max_dd = run_fast_sim(sig, inst, params, ctx)
            except Exception as ex:
                continue
            stats = compute_stats(closed, final_bal, max_dd, len(bars))
            sc    = score_result(stats)
            all_results.append((sc, params, stats))

            if idx % 9 == 0 or idx == total:
                elapsed = time.time() - t_all
                best_pf = max((r[2]["pf"] for r in all_results), default=0)
                print(f"  [{idx:>3}/{total}]  {elapsed:.0f}s elapsed  "
                      f"best PF so far: {best_pf:.2f}")

        print()
        all_results.sort(key=lambda x: x[0], reverse=True)
        profitable = [r for r in all_results if r[2]["pf"] > 1.0 and r[2]["trades"] >= 50]

        print(f"  TOP 5 FOR {symbol}")
        print(f"  {'-'*76}")
        print(f"  {'#':<3} {'Session':<12} {'Bias':<6} {'Wick':<6} {'TP':<5} "
              f"{'Trades':<8} {'WR%':<7} {'PF':<6} {'Net$':<9} {'DD%':<7} {'T/Day'}")
        print(f"  {'-'*76}")
        shown = 0
        for (sc, p, s) in all_results:
            if shown >= 5 or s["trades"] < 20:
                break
            tag = " <-- BEST" if shown == 0 else ""
            print(f"  {shown+1:<3} {p['session_mode']:<12} "
                  f"{'Strict' if p['bias_strict'] else 'Loose ':<6} "
                  f"{p['reject_wick']:<6.2f} {p['tp_rr']:<5.1f} "
                  f"{s['trades']:<8} {s['wr']:<7.1f} {s['pf']:<6.2f} "
                  f"{s['net']:<9.0f} {s['max_dd']:<7.1f} {s['tpd']:.2f}{tag}")
            shown += 1

        best_cfg = profitable[0] if profitable else all_results[0] if all_results else None
        if best_cfg:
            _, bp, bs = best_cfg
            print(f"\n  BEST CONFIG for {symbol}:")
            print(f"    session_mode = \"{bp['session_mode']}\"  "
                  f"bias_strict = {bp['bias_strict']}  "
                  f"reject_wick = {bp['reject_wick']}  "
                  f"tp_rr = {bp['tp_rr']}")
            print(f"    Trades={bs['trades']}  WR={bs['wr']}%  PF={bs['pf']:.2f}  "
                  f"Net=${bs['net']:.0f}  MaxDD={bs['max_dd']:.1f}%  T/Day={bs['tpd']:.2f}")
            best_per_inst[symbol] = (bp, bs)

    mt5.shutdown()

    print(f"\n\n{SEP2}")
    print(f"  FINAL SUMMARY -- BEST CONFIG PER PAIR")
    print(f"{SEP2}")
    print(f"\n  {'Pair':<8} {'Session':<12} {'Bias':<7} {'Wick':<6} {'TP':<5} "
          f"{'Trades':<8} {'WR%':<7} {'PF':<6} {'Net$':<9} {'DD%':<7} {'T/Day':<7} {'Use?'}")
    print(f"  {'-'*90}")
    for sym, (p, s) in best_per_inst.items():
        use = "YES" if s["pf"] >= 1.15 and s["max_dd"] <= 35 else "MAYBE" if s["pf"] >= 1.05 else "NO"
        print(f"  {sym:<8} {p['session_mode']:<12} "
              f"{'Strict' if p['bias_strict'] else 'Loose ':<7} "
              f"{p['reject_wick']:<6.2f} {p['tp_rr']:<5.1f} "
              f"{s['trades']:<8} {s['wr']:<7.1f} {s['pf']:<6.2f} "
              f"{s['net']:<9.0f} {s['max_dd']:<7.1f} {s['tpd']:<7.2f} {use}")
    print(f"\n{SEP2}\n")


if __name__ == "__main__":
    main()
