#!/usr/bin/env python3
"""
scripts/backtest_h_btc.py  -  Strategy H adapted for BTCUSD (24/7 market)
==========================================================================
Run:  py -3 scripts/backtest_h_btc.py

Goal:
  * Measure Strategy H edge on BTCUSD over a 2-year M15 backtest
  * Find optimal kill-zone session window for BTC (Asia/London/US)
  * Show weekend-only stats (Sat+Sun) -- the main use case vs XAUUSD

Key BTC differences vs XAUUSD:
  * 24/7 market (no weekend closure)
  * 3 kill zones: Asia 01-04 UTC, London 07-10 UTC, US 13-16 UTC
  * All ATR-normalised thresholds auto-scale to BTC's higher volatility
  * pip_size=1.0 ($1 per pip), 1 lot = 1 BTC
"""

import sys, os, itertools, time, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

# ── BTC instrument params ─────────────────────────────────────────────────────
INST = {
    "pip_size":   1.0,    # $1 per pip
    "pip_val":    1.0,    # $ per lot per pip  (1 lot = 1 BTC)
    "spread_pts": 20.0,   # typical $20 spread on demo BTC
    "slippage":   5.0,    # $5 slippage
}

SYMBOL_CANDIDATES = ["BTCUSD", "Bitcoin", "BTCUSD.", "BTC/USD", "XBTUSD"]

# ── BTC Kill Zones (UTC hours) ────────────────────────────────────────────────
KZ_ASIA   = set(range(1, 5))     # 01:00-04:59 UTC  Asian crypto session
KZ_LONDON = set(range(7, 10))    # 07:00-09:59 UTC  London open
KZ_US     = set(range(13, 16))   # 13:00-15:59 UTC  US open

SESSION_CONFIGS = {
    "all_24h":   set(range(0, 24)),          # no filter
    "all_3kz":   KZ_ASIA | KZ_LONDON | KZ_US,
    "us_london": KZ_LONDON | KZ_US,
    "us_only":   KZ_US,
}

# ── Outer grid (signal rebuild per combo) ─────────────────────────────────────
OUTER_BOS_CARRY = [25, 35, 50]
OUTER_EQL_TOL   = [0.80, 1.20, 1.60]

# ── Inner grid (sim-only, no rebuild) ────────────────────────────────────────
INNER_REJECT_WICK = [0.15, 0.20, 0.25]
INNER_SWEEP_MIN   = [0.10, 0.15, 0.20]
INNER_TP_RR       = [2.0, 2.5, 3.0]
INNER_SESSION     = list(SESSION_CONFIGS.keys())

# ── Sim constants ─────────────────────────────────────────────────────────────
INIT_BAL         = 1000.0
RISK_PCT         = 0.01
RISK_PCT_REDUCED = 0.005
DAILY_LOSS_LIMIT = 0.02
CONSEC_REDUCE    = 3
CONSEC_HALT      = 6
WARMUP           = 300
TIME_STOP_BARS   = 64
BOS_WINDOW       = 20
FLOAT_EPS        = 1e-9


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pnl(direction, entry, exit_p, lot):
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / INST["pip_size"] * INST["pip_val"] * lot, 4)


def _compute_sl(entry, direction, atr, sweep=None):
    if direction == "long":
        ref = min(sweep, entry - atr * 0.5) if sweep else entry - atr * 0.5
        return round(ref - atr * 0.3, 2)
    else:
        ref = max(sweep, entry + atr * 0.5) if sweep else entry + atr * 0.5
        return round(ref + atr * 0.3, 2)


def _compute_tp1(entry, sl, direction, rr):
    dist = abs(entry - sl)
    return (entry + dist * rr) if direction == "long" else (entry - dist * rr)


def _lot(balance, sl_pts, consec):
    rp  = RISK_PCT_REDUCED if consec >= CONSEC_REDUCE else RISK_PCT
    raw = (balance * rp) / (max(sl_pts, 0.01) / INST["pip_size"] * INST["pip_val"])
    return max(0.001, round(raw, 3))


# ── Symbol discovery ──────────────────────────────────────────────────────────

def find_btc_symbol():
    for sym in SYMBOL_CANDIDATES:
        info = mt5.symbol_info(sym)
        if info is not None:
            mt5.symbol_select(sym, True)
            return sym
    syms = [s.name for s in (mt5.symbols_get() or [])
            if "btc" in s.name.lower() or "bitcoin" in s.name.lower()]
    return syms[0] if syms else None


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


# ── Daily context (vectorised, works for 24/7 BTC) ───────────────────────────

def precompute_daily_context(bars):
    OHLCV = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d1_all = bars.resample("1D").agg(OHLCV).dropna()
    h4_all = bars.resample("4h").agg(OHLCV).dropna()

    hours       = bars.index.hour.values
    dates_norm  = bars.index.normalize()
    session_dates = np.where(hours >= 22,
                             dates_norm + pd.Timedelta(days=1), dates_norm)
    b2  = bars.copy(); b2["_sd"] = session_dates; b2["_hr"] = hours
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


# ── Signal arrays ─────────────────────────────────────────────────────────────

def build_signal_arrays(bars, bos_carry_max=35, eql_tol_atr=1.20):
    h = bars["high"].values.astype(np.float64)
    l = bars["low"].values.astype(np.float64)
    c = bars["close"].values.astype(np.float64)
    o = bars["open"].values.astype(np.float64)
    n = len(bars)

    prev_c = np.empty(n); prev_c[0] = c[0]; prev_c[1:] = c[:-1]
    tr  = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    atr = np.full(n, np.nan)
    for i in range(14, n):
        atr[i] = tr[i-14:i].mean()

    body        = np.abs(c - o)
    rng         = np.maximum(h - l, FLOAT_EPS)
    lo_c        = np.minimum(o, c); hi_c = np.maximum(o, c)
    wick_long   = (lo_c - l) / rng
    wick_short  = (h - hi_c) / rng

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

    EQL_WINDOW    = 12
    eql_sweep_ext = np.full(n, np.nan)
    eqh_sweep_ext = np.full(n, np.nan)

    for i in range(EQL_WINDOW + 2, n):
        if np.isnan(atr[i]):
            continue
        w   = slice(i - EQL_WINDOW, i - 1)
        w_l = l[w]; w_h = h[w]
        if w_l.std() < eql_tol_atr * atr[i]:
            cluster_l = w_l.min()
            ext = cluster_l - l[i]
            if ext > 0 and c[i] > cluster_l:
                eql_sweep_ext[i] = ext / atr[i]
        if w_h.std() < eql_tol_atr * atr[i]:
            cluster_h = w_h.max()
            ext = h[i] - cluster_h
            if ext > 0 and c[i] < cluster_h:
                eqh_sweep_ext[i] = ext / atr[i]

    return {
        "atr":           atr,
        "wick_long":     wick_long,
        "wick_short":    wick_short,
        "bos_long_lvl":  bos_long_lvl,
        "bos_short_lvl": bos_short_lvl,
        "eql_ext":       eql_sweep_ext,
        "eqh_ext":       eqh_sweep_ext,
        "hour":          np.array(bars.index.hour),
        "weekday":       np.array(bars.index.weekday),   # 5=Sat, 6=Sun
        "open":  o, "high": h, "low": l, "close": c,
        "dates": np.array(bars.index.date),
    }


# ── Fast simulation ───────────────────────────────────────────────────────────

def run_sim(sig, params, ctx):
    wick_thr   = params["reject_wick"]
    sweep_min  = params["sweep_min_atr"]
    tp_rr      = params["tp_rr"]
    kz_hours   = SESSION_CONFIGS[params["session"]]
    spread_pts = INST["spread_pts"]
    slippage   = INST["slippage"]
    n          = len(sig["close"])

    balance  = INIT_BAL
    peak_bal = INIT_BAL
    max_dd   = 0.0
    position = None
    pending  = None
    closed   = []
    daily_pnl = 0.0
    prev_date = None
    consec    = 0
    halt_cd   = 0

    for i in range(WARMUP, n):
        date    = sig["dates"][i]
        hour    = int(sig["hour"][i])
        weekday = int(sig["weekday"][i])
        atr     = sig["atr"][i]
        c_      = sig["close"][i]
        h_      = sig["high"][i]
        l_      = sig["low"][i]
        o_      = sig["open"][i]

        if np.isnan(atr) or atr <= 0:
            continue
        if date != prev_date:
            prev_date = date; daily_pnl = 0.0

        ctx_d      = ctx.get(date, {})
        d1_regime  = ctx_d.get("d1_regime",  "range")
        daily_bias = ctx_d.get("daily_bias",  BIAS_NEUTRAL)
        asian_h    = ctx_d.get("asian_h")
        asian_l    = ctx_d.get("asian_l")

        # Manage open position
        if position is not None:
            pos = position; d = pos["d"]; bh = i - pos["bar"]
            pos["best"] = max(pos["best"], h_) if d == "long" else min(pos["best"], l_)

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
                closed.append({"pnl": pnl, "d": d, "pat": pos["pat"],
                                "exit": "SL", "wd": pos["weekday"]})
                position = None; pending = None; continue

            tp1_hit = (not pos["tp1_done"] and
                       ((d == "long"  and h_ >= pos["tp1"]) or
                        (d == "short" and l_ <= pos["tp1"])))
            if tp1_hit:
                half    = max(0.001, round(pos["lot"] * 0.5, 3))
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
                closed.append({"pnl": pnl, "d": d, "pat": pos["pat"],
                                "exit": "TIME", "wd": pos["weekday"]})
                position = None; continue

        # Fill pending
        if pending is not None and position is None:
            p = pending
            if i > p["exp"]:
                pending = None
            else:
                fp  = o_ + slippage if p["d"] == "long" else o_ - slippage
                fp += spread_pts/2  if p["d"] == "long" else -spread_pts/2
                sl  = _compute_sl(fp, p["d"], p["atr"], p.get("sw"))
                tp1 = _compute_tp1(fp, sl, p["d"], tp_rr)
                dist = abs(fp - sl)
                if dist >= 0.3 * atr:
                    lot = _lot(balance, dist, consec)
                    position = {
                        "d": p["d"], "entry": fp, "sl": sl, "tp1": tp1,
                        "lot": lot, "bar": i,
                        "tp1_done": False, "tp1_pnl": 0.0,
                        "half": max(0.001, round(lot*0.5, 3)),
                        "pat": p["pat"], "atr": p["atr"], "best": fp,
                        "weekday": weekday,
                    }
                    pending = None

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

        # Kill zone filter
        if hour not in kz_hours:
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

            pat = None; entry = None; sweep = None

            if direction == "long" and not np.isnan(eql) and eql >= sweep_min:
                pat = "C"; entry = c_; sweep = l_
            elif direction == "short" and not np.isnan(eqh) and eqh >= sweep_min:
                pat = "C"; entry = c_; sweep = h_

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

    if position is not None:
        pos = position; d = pos["d"]
        rem = pos["half"] if pos["tp1_done"] else pos["lot"]
        pnl = _pnl(d, pos["entry"], sig["close"][-1], rem)
        if pos["tp1_done"]: pnl += pos["tp1_pnl"]
        balance += pnl
        closed.append({"pnl": pnl, "d": d, "pat": pos["pat"],
                        "exit": "END", "wd": pos["weekday"]})

    return closed, balance, max_dd


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(closed, final_bal, max_dd, n_bars, label=""):
    if not closed:
        return {"trades":0,"wr":0,"pf":0,"net":0,"max_dd":max_dd,"tpd":0,
                "pat_B":0,"pat_C":0,"label":label}
    wins   = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    tdays = n_bars / (24 * 4)
    return {
        "trades": len(closed),
        "wr":     round(len(wins) / len(closed) * 100, 1),
        "pf":     round(gw / gl, 2) if gl > 0 else 9.99,
        "net":    round(gw - gl, 2),
        "max_dd": round(max_dd, 1),
        "tpd":    round(len(closed) / max(tdays, 1), 2),
        "pat_B":  sum(1 for t in closed if t["pat"] == "B"),
        "pat_C":  sum(1 for t in closed if t["pat"] == "C"),
        "label":  label,
    }


def weekend_stats(closed):
    """Split trades into weekend (Sat=5, Sun=6) vs weekday."""
    wkend = [t for t in closed if t.get("wd", 0) in (5, 6)]
    wkday = [t for t in closed if t.get("wd", 0) not in (5, 6)]

    def _s(trades):
        if not trades: return "  0 trades"
        wins = [t for t in trades if t["pnl"] > 0]
        gw = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0))
        wr = len(wins)/len(trades)*100
        pf = gw/gl if gl > 0 else 9.99
        net = gw - gl
        return (f"  {len(trades):>4} trades  WR={wr:.1f}%  PF={pf:.2f}"
                f"  Net=${net:>8.0f}")

    return _s(wkend), _s(wkday)


def score(s):
    if s["trades"] < 100 or s["max_dd"] > 45:
        return -999.0
    return ((s["pf"] - 1.0) * 100 +
            (s["wr"]  - 40.0) * 2 -
            max(0.0, s["max_dd"] - 25.0) * 3 +
            math.log(max(s["trades"], 1)) * 4)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    SEP  = "-" * 82
    SEP2 = "=" * 82

    print(f"\n{SEP2}")
    print(f"  STRATEGY H  --  BTCUSD  (24/7 market)  |  2-Year M15 Backtest")
    print(f"  Testing 3 kill zones: Asia 01-04, London 07-10, US 13-16 UTC")
    print(f"  Weekend Sat+Sun stats reported separately (XAUUSD offline hours)")
    print(f"{SEP2}\n")

    if not mt5.initialize(login=config.LOGIN, password=config.PASSWORD,
                          server=config.SERVER):
        print(f"  MT5 init failed: {mt5.last_error()}"); return

    print("  Searching for BTCUSD symbol...", end="", flush=True)
    symbol = find_btc_symbol()
    if symbol is None:
        print("\n  ERROR: BTCUSD not found on this broker.")
        print("  Available crypto symbols:")
        for s in (mt5.symbols_get() or []):
            if any(k in s.name.lower() for k in ("btc","xbt","bitcoin","crypto")):
                print(f"    {s.name}")
        mt5.shutdown(); return
    print(f" found: {symbol}")

    info = mt5.symbol_info(symbol)
    print(f"  Contract: 1 lot = {info.trade_contract_size} BTC"
          f"  |  Spread ~{info.spread} pts"
          f"  |  Min lot {info.volume_min}")

    print(f"\n  Fetching 2-year M15 data...", end="", flush=True)
    bars = fetch_m15(symbol, years=2)
    n_bars = len(bars)
    weekend_bars = ((bars.index.weekday == 5) | (bars.index.weekday == 6)).sum()
    print(f" {n_bars:,} bars  ({bars.index[0].date()} to {bars.index[-1].date()})")
    print(f"  Weekend bars: {weekend_bars:,}  ({weekend_bars/n_bars*100:.1f}% of total)")

    print(f"\n  Pre-computing daily context (BTC 24/7)...", end="", flush=True)
    t0 = time.time()
    ctx = precompute_daily_context(bars)
    print(f" {time.time()-t0:.1f}s  ({len(ctx)} dates)")

    # ── Quick baseline (all_24h, tuned XAUUSD params) ────────────────────────
    print(f"\n  [BASELINE] carry=35  eql=1.20  wick=0.20  sweep=0.15  tp=2.0  session=all_24h")
    sig_base = build_signal_arrays(bars, bos_carry_max=35, eql_tol_atr=1.20)
    base_params = {"reject_wick": 0.20, "sweep_min_atr": 0.15, "tp_rr": 2.0, "session": "all_24h"}
    cl_b, bal_b, dd_b = run_sim(sig_base, base_params, ctx)
    st_b = compute_stats(cl_b, bal_b, dd_b, n_bars)
    we_str, wd_str = weekend_stats(cl_b)
    print(f"  Overall  => Trades={st_b['trades']:>4}  T/Day={st_b['tpd']:.2f}  "
          f"WR={st_b['wr']}%  PF={st_b['pf']:.2f}  Net=${st_b['net']:>8.0f}  DD={st_b['max_dd']:.1f}%")
    print(f"  Weekday  =>{wd_str}")
    print(f"  Weekend  =>{we_str}")

    # ── Grid search ───────────────────────────────────────────────────────────
    outer_combos = list(itertools.product(OUTER_BOS_CARRY, OUTER_EQL_TOL))
    inner_combos = list(itertools.product(INNER_REJECT_WICK, INNER_SWEEP_MIN,
                                          INNER_TP_RR, INNER_SESSION))
    total = len(outer_combos) * len(inner_combos)

    print(f"\n  Grid: {len(outer_combos)} signal builds x {len(inner_combos)} sims = {total} combos")
    print(f"  Running grid search...\n")

    all_results = []
    sim_n = 0
    t_all = time.time()

    for carry, eql_tol in outer_combos:
        sig = build_signal_arrays(bars, bos_carry_max=carry, eql_tol_atr=eql_tol)
        for wick, sweep_min, tprr, sess in inner_combos:
            params = {"reject_wick": wick, "sweep_min_atr": sweep_min,
                      "tp_rr": tprr, "session": sess}
            closed, final_bal, max_dd = run_sim(sig, params, ctx)
            st = compute_stats(closed, final_bal, max_dd, n_bars)
            we, wd = weekend_stats(closed)
            wkend_trades = [t for t in closed if t.get("wd", 0) in (5, 6)]
            wkend_wins   = [t for t in wkend_trades if t["pnl"] > 0]
            wkend_gl     = abs(sum(t["pnl"] for t in wkend_trades if t["pnl"] <= 0))
            wkend_gw     = sum(t["pnl"] for t in wkend_wins)
            wkend_pf     = round(wkend_gw / wkend_gl, 2) if wkend_gl > 0 else 9.99
            wkend_wr     = round(len(wkend_wins)/max(len(wkend_trades),1)*100, 1)
            all_results.append({
                "carry": carry, "eql": eql_tol, "wick": wick,
                "sweep": sweep_min, "tp": tprr, "sess": sess,
                **st,
                "wkend_n": len(wkend_trades),
                "wkend_wr": wkend_wr,
                "wkend_pf": wkend_pf,
                "wkend_net": round(wkend_gw - wkend_gl, 2),
                "sc": score(st),
            })
            sim_n += 1

        done = outer_combos.index((carry, eql_tol)) + 1
        best_pf = max((r["pf"] for r in all_results if r["trades"] >= 50), default=0)
        print(f"  Build {done:>2}/{len(outer_combos)}  carry={carry}  eql={eql_tol}  "
              f"{sim_n}/{total} sims  {time.time()-t_all:.0f}s  "
              f"best PF so far: {best_pf:.2f}")

    # ── Sort + display ─────────────────────────────────────────────────────────
    all_results.sort(key=lambda x: x["sc"], reverse=True)
    viable = [r for r in all_results if r["pf"] >= 1.10 and r["trades"] >= 100
              and r["max_dd"] <= 40]

    print(f"\n\n{SEP2}")
    print(f"  RESULTS  --  All times on BTCUSD M15  (PF >= 1.10, DD <= 40%)")
    print(f"{SEP2}")
    hdr = (f"  {'Sess':<10} {'C':<4} {'E':<5} {'W':<5} {'Sw':<5} {'TP':<4}"
           f"  {'T/D':<5} {'Trd':<5} {'WR%':<6} {'PF':<5} {'Net$':<9}"
           f"  {'DD%':<5} {'WeN':<5} {'WeWR':<6} {'WePF'}")
    print(hdr); print(f"  {'-'*78}")

    shown = viable[:15] if viable else all_results[:15]
    tag = "VIABLE" if viable else "BEST OVERALL (none met filter)"
    print(f"  --- {tag} ---")

    for rank, r in enumerate(shown, 1):
        tag2 = " <-- BEST" if rank == 1 else ""
        print(f"  {r['sess']:<10} {r['carry']:<4} {r['eql']:<5.2f} {r['wick']:<5.2f}"
              f" {r['sweep']:<5.2f} {r['tp']:<4.1f}"
              f"  {r['tpd']:<5.2f} {r['trades']:<5} {r['wr']:<6.1f}"
              f" {r['pf']:<5.2f} {r['net']:<9.0f}"
              f"  {r['max_dd']:<5.1f} {r['wkend_n']:<5}"
              f" {r['wkend_wr']:<6.1f} {r['wkend_pf']:.2f}{tag2}")

    # ── Best config deep-dive ──────────────────────────────────────────────────
    best = shown[0] if shown else None
    if best:
        print(f"\n{SEP2}")
        print(f"  BEST CONFIG DETAIL  --  session={best['sess']}")
        print(f"{SEP2}")
        # Re-run best to get trade list
        sig_best = build_signal_arrays(bars, bos_carry_max=best["carry"],
                                       eql_tol_atr=best["eql"])
        bp = {"reject_wick": best["wick"], "sweep_min_atr": best["sweep"],
              "tp_rr": best["tp"], "session": best["sess"]}
        cl_best, bal_best, dd_best = run_sim(sig_best, bp, ctx)
        we_b, wd_b = weekend_stats(cl_best)

        print(f"\n  Overall Performance:")
        print(f"    Trades : {best['trades']}  ({best['tpd']:.2f}/day)")
        print(f"    Win Rate: {best['wr']:.1f}%")
        print(f"    Prof Factor: {best['pf']:.2f}")
        print(f"    Net P&L: ${best['net']:.0f}  on $1,000 account")
        print(f"    Max DD : {best['max_dd']:.1f}%")
        print(f"    Pattern B: {best['pat_B']}  |  Pattern C: {best['pat_C']}")

        print(f"\n  Weekday (Mon-Fri):  {wd_b}")
        print(f"  Weekend (Sat-Sun):  {we_b}")

        # Session breakdown
        for sess_name in SESSION_CONFIGS:
            sess_trades = [t for t in cl_best
                          if t.get("wd", 0) in (5, 6)]  # weekend filter
            pass  # already printed above

        print(f"\n  Kill Zone Breakdown (best config, all days):")
        for kz_name, kz_hrs in [("Asia  01-04", KZ_ASIA),
                                  ("London 07-10", KZ_LONDON),
                                  ("US    13-16", KZ_US)]:
            kz_cl = [t for t in cl_best]  # can't filter by hour post-sim
            print(f"    {kz_name} : included in session={best['sess']}: "
                  f"{'YES' if kz_hrs.issubset(SESSION_CONFIGS[best['sess']]) else 'NO'}")

        print(f"\n  Verdict:")
        if best["pf"] >= 1.20:
            print(f"  [GOOD] BTCUSD has edge with Strategy H  (PF={best['pf']:.2f})")
            print(f"  Safe to run alongside XAUUSD (XAUUSD weekdays, BTC 24/7).")
        elif best["pf"] >= 1.10:
            print(f"  [MARGINAL] Edge exists but thin  (PF={best['pf']:.2f})")
            print(f"  Consider running BTC weekend-only until more data confirms edge.")
        else:
            print(f"  [NO EDGE] Best PF={best['pf']:.2f} -- Strategy H does not suit BTC.")
            print(f"  BTC structure may need different pattern logic.")

        print(f"\n  Config to use in live bot:")
        print(f"    symbol        = {symbol}")
        print(f"    session_hours = {sorted(SESSION_CONFIGS[best['sess']])}")
        print(f"    REJECT_WICK   = {best['wick']}")
        print(f"    EQL_TOL_ATR   = {best['eql']:.2f}")
        print(f"    SWEEP_MIN_ATR = {best['sweep']:.2f}")
        print(f"    BOS_RETEST_MAX= {best['carry']}")
        print(f"    TP_RR         = {best['tp']:.1f}")

    print(f"\n{SEP2}\n")
    mt5.shutdown()


if __name__ == "__main__":
    main()
