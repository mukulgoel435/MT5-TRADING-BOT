#!/usr/bin/env python3
"""
scripts/tune_xauusd.py
======================
Tune Strategy H XAUUSD parameters to reach 2-3 trades/day.

Current baseline: ~1.59 trades/day, PF 1.20, WR 48.4%, MaxDD 21.9%

Levers (in order of impact on trade frequency):
  reject_wick   : 0.25→0.15  fewer bars disqualified = more Pattern B
  bos_carry_max : 25→40      BOS level stays valid longer = more retests found
  eql_tol_atr   : 0.8→1.2    wider equal-level clusters = more Pattern C
  sweep_min_atr : 0.30→0.15  smaller sweeps count = more Pattern C
  tp_rr         : 2.0→2.5    does not change frequency but keeps PF healthy

Grid: 3 × 3 × 3 = 27 signal builds × 3 × 2 = 18 sim runs each = 486 total
Estimated time: ~45 seconds

Usage:
    py -3 scripts/tune_xauusd.py
"""

import sys, os, itertools, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import config

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_CHOP, D1_COMPRESS,
    H4_NONE, BIAS_NEUTRAL,
)
from src.strategy_h.sizing import compute_lot

# ── XAUUSD instrument config ──────────────────────────────────────────────────
INST = {
    "pip_size":   0.1,
    "pip_val":    10.0,
    "spread_pts": 0.15,
    "slippage":   0.05,
}

# ── Baseline (current live config) ────────────────────────────────────────────
BASELINE = {
    "bos_carry_max": 25,
    "eql_tol_atr":   0.80,
    "reject_wick":   0.25,
    "sweep_min_atr": 0.30,
    "tp_rr":         2.0,
    "label":         "BASELINE",
}

# ── Outer grid (requires signal rebuild) ─────────────────────────────────────
OUTER_BOS_CARRY  = [25, 35, 50]       # bars BOS level stays valid for retest
OUTER_EQL_TOL    = [0.80, 1.00, 1.20] # equal-level cluster tolerance (× ATR)

# ── Inner grid (simulation only, no rebuild) ──────────────────────────────────
INNER_REJECT_WICK   = [0.15, 0.20, 0.25]  # min rejection wick fraction
INNER_SWEEP_MIN     = [0.15, 0.20, 0.30]  # min sweep extension (× ATR)
INNER_TP_RR         = [2.0, 2.5]           # take-profit R:R

# ── Shared sim constants ──────────────────────────────────────────────────────
INIT_BAL         = 1000.0
RISK_PCT         = 0.01
RISK_PCT_REDUCED = 0.005
DAILY_LOSS_LIMIT = 0.02
CONSEC_REDUCE    = 3
CONSEC_HALT      = 6
WARMUP           = 250
TIME_STOP_BARS   = 64    # 16 hours
BOS_WINDOW       = 20    # rolling window to find swing high/low
FLOAT_EPS        = 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pnl(direction, entry, exit_p, lot):
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / INST["pip_size"] * INST["pip_val"] * lot, 4)


def _compute_sl(entry, direction, atr, sweep=None):
    if direction == "long":
        ref = min(sweep, entry - atr * 0.5) if sweep else entry - atr * 0.5
        return round(ref - atr * 0.3, 4)
    else:
        ref = max(sweep, entry + atr * 0.5) if sweep else entry + atr * 0.5
        return round(ref + atr * 0.3, 4)


def _compute_tp1(entry, sl, direction, rr):
    dist = abs(entry - sl)
    return (entry + dist * rr) if direction == "long" else (entry - dist * rr)


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_m15(symbol="XAUUSD", years=2):
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


# ── Daily context (vectorised) ────────────────────────────────────────────────

def precompute_daily_context(bars):
    OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4_all = bars.resample("4h").agg(OHLCV_AGG).dropna()

    hours = bars.index.hour.values
    dates_norm = bars.index.normalize()
    session_dates = np.where(hours >= 22, dates_norm + pd.Timedelta(days=1), dates_norm)
    b2 = bars.copy(); b2["_sd"] = session_dates; b2["_hr"] = hours
    amask = (hours >= 22) | (hours < 7)
    if amask.any():
        ab = b2[amask].groupby("_sd").agg({"high": "max", "low": "min"})
        asian_cache = {pd.Timestamp(k).date(): (float(h), float(l))
                       for k, h, l in zip(ab.index, ab["high"].values, ab["low"].values)}
    else:
        asian_cache = {}

    open7 = (b2[b2["_hr"] == 7]
             .groupby(b2[b2["_hr"] == 7].index.date)["close"].first())

    d1_dates = np.array([t.date() for t in d1_all.index])
    h4_idx   = h4_all.index
    ctx = {}

    for date in sorted(set(bars.index.date)):
        n_d1 = int((d1_dates < date).sum())
        if n_d1 < 2:
            continue
        d1_b = d1_all.iloc[:n_d1]
        pdh  = float(d1_b.iloc[-1]["high"])
        pdl  = float(d1_b.iloc[-1]["low"])
        pdc  = float(d1_b.iloc[-1]["close"])
        rec  = d1_b.iloc[-min(20, n_d1):]
        d1rh = float(rec["high"].max()); d1rl = float(rec["low"].min())
        d1_regime = compute_d1_regime(d1_b) if n_d1 >= 12 else "range"

        h4_cut = pd.Timestamp(year=date.year, month=date.month,
                               day=date.day, hour=7, tzinfo=timezone.utc)
        h4_msk = h4_idx + pd.Timedelta(hours=4) <= h4_cut
        h4_dir = compute_h4_direction(h4_all[h4_msk]) if h4_msk.sum() >= 12 else H4_NONE

        price = float(open7.get(date, pdc))
        daily_bias, _ = compute_daily_bias(
            d1_regime, h4_dir, price, d1rh, d1rl, pdh, pdl, pdc)

        asian = asian_cache.get(date, (None, None))
        ctx[date] = {
            "d1_regime": d1_regime, "h4_dir": h4_dir, "daily_bias": daily_bias,
            "d1rh": d1rh, "d1rl": d1rl, "pdh": pdh, "pdl": pdl, "pdc": pdc,
            "asian_h": asian[0], "asian_l": asian[1],
        }
    return ctx


# ── Signal arrays (parameterised) ────────────────────────────────────────────

def build_signal_arrays(bars, bos_carry_max=25, eql_tol_atr=0.80):
    """
    Build signal arrays with tunable BOS carry length and EQL cluster tolerance.
    Stores ALL sweep extensions (no SWEEP_MIN filter) so they can be filtered
    per-simulation without rebuilding.
    """
    h = bars["high"].values.astype(np.float64)
    l = bars["low"].values.astype(np.float64)
    c = bars["close"].values.astype(np.float64)
    o = bars["open"].values.astype(np.float64)
    n = len(bars)

    # ATR (14-period)
    prev_c = np.empty(n); prev_c[0] = c[0]; prev_c[1:] = c[:-1]
    tr  = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = np.full(n, np.nan)
    for i in range(14, n):
        atr[i] = tr[i-14:i].mean()

    body = np.abs(c - o)
    rng  = np.maximum(h - l, FLOAT_EPS)

    # Rejection wicks
    lo_c = np.minimum(o, c);  hi_c = np.maximum(o, c)
    wick_long  = (lo_c - l) / rng
    wick_short = (h - hi_c) / rng

    # BOS: close crosses 20-bar swing high/low with body >= 0.40 × ATR
    # Carry forward the BOS level for bos_carry_max bars
    bos_long_lvl  = np.full(n, np.nan)
    bos_short_lvl = np.full(n, np.nan)
    carry_l = np.nan; carry_s = np.nan
    carry_l_bar = -9999; carry_s_bar = -9999

    for i in range(BOS_WINDOW + 1, n):
        if np.isnan(atr[i]):
            continue
        swing_h = h[i - BOS_WINDOW: i].max()
        swing_l = l[i - BOS_WINDOW: i].min()

        if c[i] > swing_h and body[i] >= 0.40 * atr[i]:
            carry_l = swing_h; carry_l_bar = i
        if c[i] < swing_l and body[i] >= 0.40 * atr[i]:
            carry_s = swing_l; carry_s_bar = i

        if i - carry_l_bar > bos_carry_max: carry_l = np.nan
        if i - carry_s_bar > bos_carry_max: carry_s = np.nan

        bos_long_lvl[i]  = carry_l
        bos_short_lvl[i] = carry_s

    # Equal level sweep — store ALL valid sweeps (filter by sweep_min in sim)
    EQL_WINDOW = 12
    eql_sweep_ext = np.full(n, np.nan)   # long (EQL sweep), in ATR units
    eqh_sweep_ext = np.full(n, np.nan)   # short (EQH sweep), in ATR units

    for i in range(EQL_WINDOW + 2, n):
        if np.isnan(atr[i]):
            continue
        w = slice(i - EQL_WINDOW, i - 1)
        w_l = l[w]; w_h = h[w]
        low_std  = w_l.std()
        high_std = w_h.std()

        if low_std < eql_tol_atr * atr[i]:
            cluster_l = w_l.min()
            ext = cluster_l - l[i]
            if ext > 0 and c[i] > cluster_l:          # swept below, closed back
                eql_sweep_ext[i] = ext / atr[i]

        if high_std < eql_tol_atr * atr[i]:
            cluster_h = w_h.max()
            ext = h[i] - cluster_h
            if ext > 0 and c[i] < cluster_h:          # swept above, closed back
                eqh_sweep_ext[i] = ext / atr[i]

    return {
        "atr":           atr,
        "rng":           rng,
        "wick_long":     wick_long,
        "wick_short":    wick_short,
        "bos_long_lvl":  bos_long_lvl,
        "bos_short_lvl": bos_short_lvl,
        "eql_ext":       eql_sweep_ext,
        "eqh_ext":       eqh_sweep_ext,
        "hour":          np.array(bars.index.hour),
        "open":          o, "high": h, "low": l, "close": c,
        "dates":         np.array(bars.index.date),
    }


# ── Fast simulation ───────────────────────────────────────────────────────────

def run_sim(sig, params, ctx):
    wick_thr   = params["reject_wick"]
    sweep_min  = params["sweep_min_atr"]
    tp_rr      = params["tp_rr"]

    pip_size   = INST["pip_size"]
    pip_val    = INST["pip_val"]
    spread_pts = INST["spread_pts"]
    slippage   = INST["slippage"]

    n          = len(sig["close"])
    balance    = INIT_BAL
    peak_bal   = INIT_BAL
    max_dd     = 0.0
    position   = None
    pending    = None
    closed     = []
    daily_pnl  = 0.0
    prev_date  = None
    consec     = 0
    halt_cd    = 0

    for i in range(WARMUP, n):
        date = sig["dates"][i]
        hour = int(sig["hour"][i])
        atr  = sig["atr"][i]
        c_   = sig["close"][i]
        h_   = sig["high"][i]
        l_   = sig["low"][i]
        o_   = sig["open"][i]

        if np.isnan(atr) or atr <= 0:
            continue
        if date != prev_date:
            prev_date = date; daily_pnl = 0.0

        ctx_d      = ctx.get(date, {})
        d1_regime  = ctx_d.get("d1_regime",  "range")
        daily_bias = ctx_d.get("daily_bias",  BIAS_NEUTRAL)
        asian_h    = ctx_d.get("asian_h")
        asian_l    = ctx_d.get("asian_l")
        h4_dir     = ctx_d.get("h4_dir",     H4_NONE)

        # ── Manage open position ───────────────────────────────────────────
        if position is not None:
            pos = position
            d   = pos["d"]
            bh  = i - pos["bar"]

            if d == "long":  pos["best"] = max(pos["best"], h_)
            else:            pos["best"] = min(pos["best"], l_)

            sl_hit = (d == "long" and l_ <= pos["sl"]) or (d == "short" and h_ >= pos["sl"])
            if sl_hit:
                fill = pos["sl"] - slippage if d == "long" else pos["sl"] + slippage
                rem  = pos["half"] if pos["tp1_done"] else pos["lot"]
                pnl  = _pnl(d, pos["entry"], fill, rem)
                if pos["tp1_done"]: pnl += pos["tp1_pnl"]
                balance += pnl; daily_pnl += pnl
                peak_bal = max(peak_bal, balance)
                max_dd   = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                consec   = 0 if pnl > 0 else consec + 1
                closed.append({"pnl": pnl, "d": d, "pat": pos["pat"], "exit": "SL"})
                position = None; pending = None; continue

            tp1_hit = (not pos["tp1_done"] and
                       ((d == "long" and h_ >= pos["tp1"]) or
                        (d == "short" and l_ <= pos["tp1"])))
            if tp1_hit:
                half    = max(0.01, round(pos["lot"] * 0.5, 2))
                tp1_pnl = _pnl(d, pos["entry"], pos["tp1"], half)
                pos.update({"tp1_done": True, "tp1_pnl": tp1_pnl,
                             "half": half, "sl": pos["entry"]})
                continue

            if pos["tp1_done"]:
                trail = (pos["best"] - 1.5 * atr if d == "long"
                         else pos["best"] + 1.5 * atr)
                if (d == "long" and trail > pos["sl"]) or \
                   (d == "short" and trail < pos["sl"]):
                    pos["sl"] = trail

            if bh >= TIME_STOP_BARS:
                fill = c_ - slippage if d == "long" else c_ + slippage
                rem  = pos["half"] if pos["tp1_done"] else pos["lot"]
                pnl  = _pnl(d, pos["entry"], fill, rem)
                if pos["tp1_done"]: pnl += pos["tp1_pnl"]
                balance += pnl; daily_pnl += pnl
                peak_bal = max(peak_bal, balance)
                max_dd   = max(max_dd, (peak_bal - balance) / peak_bal * 100)
                consec   = 0 if pnl > 0 else consec + 1
                closed.append({"pnl": pnl, "d": d, "pat": pos["pat"], "exit": "TIME"})
                position = None; continue

        # ── Fill pending ───────────────────────────────────────────────────
        if pending is not None and position is None:
            p = pending
            if i > p["exp"]:
                pending = None
            else:
                fp = None
                if p["otype"] == "market":
                    fp = o_ + slippage if p["d"] == "long" else o_ - slippage
                if fp is not None:
                    fp  += spread_pts/2 if p["d"] == "long" else -spread_pts/2
                    sl   = _compute_sl(fp, p["d"], p["atr"], p.get("sw"))
                    tp1  = _compute_tp1(fp, sl, p["d"], tp_rr)
                    dist = abs(fp - sl)
                    if dist >= 0.3 * atr:
                        risk = RISK_PCT_REDUCED if consec >= CONSEC_REDUCE else RISK_PCT
                        lot  = compute_lot(balance, dist, risk)
                        position = {
                            "d": p["d"], "entry": fp, "sl": sl, "tp1": tp1,
                            "lot": lot, "bar": i,
                            "tp1_done": False, "tp1_pnl": 0.0,
                            "half": max(0.01, round(lot*0.5, 2)),
                            "pat": p["pat"], "atr": p["atr"], "best": fp,
                        }
                        pending = None

        # ── Signal detection ───────────────────────────────────────────────
        if position is not None or pending is not None:
            continue
        if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
            continue
        if consec >= CONSEC_HALT:
            if halt_cd == 0: halt_cd = i + 96
            if i < halt_cd: continue
            else: consec = CONSEC_REDUCE; halt_cd = 0
        if d1_regime in (D1_CHOP, D1_COMPRESS):
            continue
        if asian_h is None or asian_l is None:
            continue

        wl  = sig["wick_long"][i]
        ws  = sig["wick_short"][i]
        bl  = sig["bos_long_lvl"][i]
        bs  = sig["bos_short_lvl"][i]
        eql = sig["eql_ext"][i]
        eqh = sig["eqh_ext"][i]

        for direction in ("long", "short"):
            if not bias_allows_direction(daily_bias, direction):
                continue

            pat   = None; entry = None; sweep = None

            # Pattern C: equal level sweep (priority)
            if direction == "long" and not np.isnan(eql) and eql >= sweep_min:
                pat = "C"; entry = c_; sweep = l_
            elif direction == "short" and not np.isnan(eqh) and eqh >= sweep_min:
                pat = "C"; entry = c_; sweep = h_

            # Pattern B: BOS retest with rejection
            if pat is None:
                if direction == "long" and not np.isnan(bl):
                    if abs(l_ - bl) <= 2.5 * atr and wl >= wick_thr:
                        pat = "B"; entry = c_; sweep = l_
                elif direction == "short" and not np.isnan(bs):
                    if abs(h_ - bs) <= 2.5 * atr and ws >= wick_thr:
                        pat = "B"; entry = c_; sweep = h_

            if pat is None:
                continue

            sl_test = _compute_sl(entry, direction, atr, sweep)
            if abs(entry - sl_test) < 0.3 * atr:
                continue

            pending = {"d": direction, "ep": entry, "sw": sweep,
                       "atr": atr, "pat": pat, "otype": "market", "exp": i + 1}
            break

    # Force-close at end
    if position is not None:
        pos = position; d = pos["d"]
        rem = pos["half"] if pos["tp1_done"] else pos["lot"]
        pnl = _pnl(d, pos["entry"], sig["close"][-1], rem)
        if pos["tp1_done"]: pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({"pnl": pnl, "d": d, "pat": pos["pat"], "exit": "END"})

    return closed, balance, max_dd


# ── Stats + score ─────────────────────────────────────────────────────────────

def compute_stats(closed, final_bal, max_dd, n_bars):
    if not closed:
        return {"trades":0,"wr":0,"pf":0,"net":0,"ret":0,"max_dd":max_dd,"tpd":0,
                "pat_B":0,"pat_C":0}
    total  = len(closed)
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pat_B = sum(1 for t in closed if t["pat"] == "B")
    pat_C = sum(1 for t in closed if t["pat"] == "C")
    tdays = n_bars / (24 * 4)
    return {
        "trades": total,
        "wr":     round(len(wins) / total * 100, 1),
        "pf":     round(gw / gl, 2) if gl > 0 else 9.99,
        "net":    round(gw - gl, 2),
        "ret":    round((final_bal - INIT_BAL) / INIT_BAL * 100, 1),
        "max_dd": round(max_dd, 1),
        "tpd":    round(total / max(tdays, 1), 2),
        "pat_B":  pat_B,
        "pat_C":  pat_C,
    }


def score(s):
    """Composite score: rewards PF, penalises DD, rewards trade count."""
    if s["trades"] < 200 or s["max_dd"] > 40:
        return -999.0
    return ((s["pf"] - 1.0) * 120 +
            (s["wr"]  - 40.0) * 2  -
            max(0.0, s["max_dd"] - 20.0) * 3 +
            math.log(max(s["trades"], 1)) * 5)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    SEP  = "-" * 82
    SEP2 = "=" * 82

    print(f"\n{SEP2}")
    print(f"  STRATEGY H -- XAUUSD FREQUENCY TUNER  |  2-Year M15")
    print(f"  Target: 2-3 trades/day  |  PF >= 1.15  |  MaxDD <= 25%")
    print(f"  Current baseline: ~1.59 T/Day, PF 1.20, WR 48.4%, MaxDD 21.9%")
    print(f"{SEP2}\n")

    if not mt5.initialize(login=config.LOGIN, password=config.PASSWORD,
                          server=config.SERVER):
        print(f"  MT5 init failed: {mt5.last_error()}"); return

    print("  Fetching XAUUSD 2-year M15 data...", end="", flush=True)
    bars = fetch_m15("XAUUSD", years=2)
    print(f" {len(bars):,} bars  ({bars.index[0].date()} to {bars.index[-1].date()})")

    print("  Pre-computing daily context...", end="", flush=True)
    t0 = time.time()
    ctx = precompute_daily_context(bars)
    print(f" {time.time()-t0:.1f}s  ({len(ctx)} dates)")

    # Run baseline first
    print(f"\n  [BASELINE] carry=25  eql_tol=0.80  wick=0.25  sweep=0.30  tp=2.0")
    sig_base = build_signal_arrays(bars, bos_carry_max=25, eql_tol_atr=0.80)
    cl_b, bal_b, dd_b = run_sim(sig_base, BASELINE, ctx)
    st_b = compute_stats(cl_b, bal_b, dd_b, len(bars))
    print(f"  Baseline => Trades={st_b['trades']}  T/Day={st_b['tpd']:.2f}  "
          f"WR={st_b['wr']}%  PF={st_b['pf']:.2f}  Net=${st_b['net']:.0f}  "
          f"DD={st_b['max_dd']:.1f}%  B={st_b['pat_B']}  C={st_b['pat_C']}")

    # ── Grid search ───────────────────────────────────────────────────────
    outer_combos = list(itertools.product(OUTER_BOS_CARRY, OUTER_EQL_TOL))
    inner_combos = list(itertools.product(INNER_REJECT_WICK, INNER_SWEEP_MIN, INNER_TP_RR))
    total_sims   = len(outer_combos) * len(inner_combos)

    print(f"\n  Grid: {len(outer_combos)} signal builds x {len(inner_combos)} sims = {total_sims} combos")
    print(f"  Running...\n")

    all_results = []
    sim_count   = 0
    t_all       = time.time()

    for carry, eql_tol in outer_combos:
        sig = build_signal_arrays(bars, bos_carry_max=carry, eql_tol_atr=eql_tol)

        for wick, sweep_min, tprr in inner_combos:
            params = {
                "reject_wick":   wick,
                "sweep_min_atr": sweep_min,
                "tp_rr":         tprr,
            }
            closed, final_bal, max_dd = run_sim(sig, params, ctx)
            stats = compute_stats(closed, final_bal, max_dd, len(bars))
            sc    = score(stats)
            all_results.append({
                "bos_carry": carry, "eql_tol": eql_tol,
                "wick": wick, "sweep_min": sweep_min, "tp_rr": tprr,
                **stats, "score": sc,
            })
            sim_count += 1

        elapsed = time.time() - t_all
        done_outer = (outer_combos.index((carry, eql_tol)) + 1)
        best_tpd = max((r["tpd"] for r in all_results), default=0)
        print(f"  Signal build {done_outer:>2}/{len(outer_combos)}  "
              f"(carry={carry} eql={eql_tol})  "
              f"{sim_count}/{total_sims} sims  "
              f"{elapsed:.0f}s  best T/Day so far: {best_tpd:.2f}")

    # ── Results ───────────────────────────────────────────────────────────
    all_results.sort(key=lambda x: x["score"], reverse=True)

    # Filter to target zone: 2.0-3.5 T/Day, PF >= 1.10, MaxDD <= 30%
    target = [r for r in all_results
              if 2.0 <= r["tpd"] <= 3.5
              and r["pf"] >= 1.10
              and r["max_dd"] <= 30.0
              and r["trades"] >= 300]

    # Also show 1.5-2.0 T/Day candidates if target zone is empty
    near    = [r for r in all_results
               if 1.5 <= r["tpd"] < 2.0
               and r["pf"] >= 1.15
               and r["max_dd"] <= 25.0] if not target else []

    print(f"\n\n{SEP2}")
    print(f"  RESULTS -- Target: 2-3 T/Day, PF >= 1.10, MaxDD <= 30%")
    print(f"{SEP2}")

    hdr = (f"  {'Carry':<7} {'EQL':<6} {'Wick':<6} {'Swp':<6} {'TP':<5} "
           f"{'T/Day':<7} {'Trades':<8} {'WR%':<7} {'PF':<6} "
           f"{'Net$':<9} {'DD%':<7} {'B/C'}")
    print(hdr)
    print(f"  {'-'*78}")

    pool = target if target else near if near else all_results[:10]
    tag  = "IN TARGET ZONE" if target else ("NEAR TARGET" if near else "BEST OVERALL")
    print(f"  --- {tag} ---")

    for rank, r in enumerate(pool[:12], 1):
        bc = f"{r['pat_B']}/{r['pat_C']}"
        tag2 = "  <-- RECOMMENDED" if rank == 1 else ""
        print(f"  {r['bos_carry']:<7} {r['eql_tol']:<6.2f} {r['wick']:<6.2f} "
              f"{r['sweep_min']:<6.2f} {r['tp_rr']:<5.1f} "
              f"{r['tpd']:<7.2f} {r['trades']:<8} {r['wr']:<7.1f} "
              f"{r['pf']:<6.2f} {r['net']:<9.0f} {r['max_dd']:<7.1f} {bc}{tag2}")

    # ── Recommendation ────────────────────────────────────────────────────
    best = pool[0] if pool else all_results[0] if all_results else None
    if best:
        print(f"\n{SEP2}")
        print(f"  RECOMMENDED CONFIG (vs baseline)")
        print(f"{SEP2}")
        print(f"")
        print(f"  Parameter        Baseline    Tuned       Change")
        print(f"  {'-'*55}")
        print(f"  bos_carry_max    25          {best['bos_carry']:<12}{'+' if best['bos_carry']>25 else '='}{abs(best['bos_carry']-25)}")
        print(f"  eql_tol_atr      0.80        {best['eql_tol']:<12.2f}{'+' if best['eql_tol']>0.80 else '='}{best['eql_tol']-0.80:+.2f}")
        print(f"  REJECT_WICK      0.25        {best['wick']:<12.2f}{best['wick']-0.25:+.2f}")
        print(f"  SWEEP_MIN_ATR    0.30        {best['sweep_min']:<12.2f}{best['sweep_min']-0.30:+.2f}")
        print(f"  tp_rr            2.0         {best['tp_rr']:<12.1f}{best['tp_rr']-2.0:+.1f}")
        print(f"")
        print(f"  Performance      Baseline    Tuned")
        print(f"  {'-'*40}")
        print(f"  Trades (2yr)     {st_b['trades']:<12}{best['trades']}")
        print(f"  Trades/day       {st_b['tpd']:<12.2f}{best['tpd']:.2f}")
        print(f"  Win Rate         {st_b['wr']:<12.1f}{best['wr']:.1f}%")
        print(f"  Profit Factor    {st_b['pf']:<12.2f}{best['pf']:.2f}")
        print(f"  Net P&L          ${st_b['net']:<11.0f}${best['net']:.0f}")
        print(f"  Max Drawdown     {st_b['max_dd']:<12.1f}{best['max_dd']:.1f}%")
        print(f"  Pattern B / C    {st_b['pat_B']}/{st_b['pat_C']:<9}{best['pat_B']}/{best['pat_C']}")
        print(f"\n  => Paste into patterns.py:")
        print(f"     REJECT_WICK    = {best['wick']}")
        print(f"     EQL_TOL_ATR    = {best['eql_tol']:.2f}")
        print(f"     SWEEP_MIN_ATR  = {best['sweep_min']:.2f}")
        print(f"     BOS_RETEST_MAX = {best['bos_carry']}")
        print(f"\n  => Also update run_live_h.py exits compute_tp1 call to use rr={best['tp_rr']}")

    print(f"\n{SEP2}\n")
    mt5.shutdown()


if __name__ == "__main__":
    main()
