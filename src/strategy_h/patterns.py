"""
src/strategy_h/patterns.py — Entry pattern detection for Strategy H.

Three patterns in descending frequency (ascending win-rate):

  Pattern C — Equal High/Low Sweep (highest win rate, ~3-4× per month)
    Equal highs or lows present → last bar swept the cluster → entry at close.
    No FVG confirmation needed; the equal-level sweep is the signal itself.

  Pattern A — Kill Zone Sweep + FVG (most frequent, highest quality)
    Within London or NY Kill Zone → sweep of session extreme (Asian H/L,
    prior day H/L) → FVG formed during sweep → entry at FVG midpoint.

  Pattern B — BOS Retest with Rejection (structural continuation)
    Break of Structure confirmed on M15 → price pulls back to BOS level
    within 10 bars → rejection candle (wick ≥40% of range) → entry.

All functions return a setup dict or None.
Setup dict keys: type, direction, entry_price, sweep_extreme, pattern_bar
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategy_f.market_structure import (
    find_swings, detect_bos, detect_liquidity_sweep,
    detect_fvg, detect_equal_levels,
)


# ── Session kill-zone hours (UTC) ────────────────────────────────────────────
LONDON_KZ = set(range(7, 10))    # 07:00–09:59 UTC
NY_KZ      = set(range(13, 16))  # 13:00–15:59 UTC

# ── Quality thresholds (tuned 2026-07-03 via tune_xauusd.py) ─────────────────
# Baseline → Tuned:  T/Day 1.59→3.26  PF 1.18→1.24  MaxDD 19.4→14.7%
FVG_MIN_ATR    = 0.30    # FVG must be >= this × ATR (filters noise)
SWEEP_MIN_ATR  = 0.15    # sweep must extend >= this × ATR beyond level  (was 0.30)
EQL_TOL_ATR    = 1.20    # equal level cluster tolerance in ATR           (was 0.80)
BOS_RETEST_MAX = 35      # bars from BOS to valid retest                  (was 25)
REJECT_WICK    = 0.20    # wick must be >= this fraction of total range   (was 0.25)


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute ATR from the last `period` bars of df."""
    if len(df) < 2:
        return 1.0
    h = df["high"].values[-period-1:]
    l = df["low"].values[-period-1:]
    c = df["close"].values[-period-1:]
    n = len(h)
    if n < 2:
        return float(h[-1] - l[-1]) if h[-1] > l[-1] else 1.0
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-min(period, len(tr)):].mean())


def _is_kill_zone(hour_utc: int) -> bool:
    return hour_utc in LONDON_KZ or hour_utc in NY_KZ


def _session_label(hour_utc: int) -> str:
    if hour_utc in LONDON_KZ:
        return "london_kz"
    if hour_utc in NY_KZ:
        return "ny_kz"
    if 7 <= hour_utc < 17:
        return "london_ny"
    return "other"


def detect_pattern_c(
    m15_df: pd.DataFrame,
    direction: str,
    atr: float,
) -> Optional[Dict]:
    """
    Pattern C: Equal High/Low sweep.

    For LONG: equal lows exist → last completed bar swept below the cluster
              and closed back above it.
    For SHORT: equal highs exist → last bar swept above cluster, closed below.

    Returns setup dict or None.
    """
    if len(m15_df) < 15 or atr <= 0:
        return None

    # Use last 25 bars to find equal levels
    lookback = min(25, len(m15_df) - 1)
    recent = m15_df.iloc[-lookback-1:-1]   # exclude forming bar; -1 = last completed
    if len(recent) < 10:
        return None

    swings = find_swings(recent, order=2)
    eq     = detect_equal_levels(swings, atr, tol=EQL_TOL_ATR)

    bar  = m15_df.iloc[-2]   # last completed bar
    high = float(bar["high"])
    low  = float(bar["low"])
    close = float(bar["close"])

    if direction == "long":
        eql = eq.get("eql")
        if eql is None:
            return None
        # Sweep: wick below eql by ≥ SWEEP_MIN_ATR, close back above eql
        wick_ext = eql - low
        if wick_ext >= SWEEP_MIN_ATR * atr and close > eql:
            return {
                "type": "C",
                "direction": "long",
                "entry_price": close,      # enter at bar close
                "sweep_extreme": low,
                "pattern_bar": len(m15_df) - 2,
                "session": "any",
                "note": f"EQL sweep at {eql:.2f}",
            }

    else:  # short
        eqh = eq.get("eqh")
        if eqh is None:
            return None
        wick_ext = high - eqh
        if wick_ext >= SWEEP_MIN_ATR * atr and close < eqh:
            return {
                "type": "C",
                "direction": "short",
                "entry_price": close,
                "sweep_extreme": high,
                "pattern_bar": len(m15_df) - 2,
                "session": "any",
                "note": f"EQH sweep at {eqh:.2f}",
            }

    return None


def detect_pattern_a(
    m15_df: pd.DataFrame,
    hour_utc: int,
    direction: str,
    asian_high: Optional[float],
    asian_low: Optional[float],
    prev_day_high: Optional[float],
    prev_day_low: Optional[float],
    atr: float,
) -> Optional[Dict]:
    """
    Pattern A: Kill Zone Sweep + FVG.

    Conditions:
      1. Within London KZ (07–09 UTC) or NY KZ (13–15 UTC)
      2. Last bar swept a session-significant level:
           - Asian session high or low
           - Prior day high or low
      3. An unmitigated FVG ≥ FVG_MIN_ATR exists in the last 5 bars
      4. The FVG direction matches the trade direction

    Returns setup dict with entry at FVG midpoint, or None.
    """
    if not _is_kill_zone(hour_utc):
        return None
    if len(m15_df) < 10 or atr <= 0:
        return None

    bar   = m15_df.iloc[-2]
    high  = float(bar["high"])
    low   = float(bar["low"])
    close = float(bar["close"])

    # Identify the target session levels
    if direction == "long":
        # Sweep of LOW levels (Asian low or prior day low)
        targets = []
        if asian_low is not None:
            targets.append(asian_low)
        if prev_day_low is not None:
            targets.append(prev_day_low)
        if not targets:
            return None

        sweep_extreme = None
        swept_level   = None
        for lvl in targets:
            ext = lvl - low
            if ext >= SWEEP_MIN_ATR * atr and close > lvl:
                sweep_extreme = low
                swept_level   = lvl
                break
        if sweep_extreme is None:
            return None

        # Look for bullish FVG in last 6 bars (includes the sweep bar)
        fvg_window = m15_df.iloc[-8:-1]
        if len(fvg_window) < 3:
            return None
        fvgs = detect_fvg(fvg_window, atr, min_size_atr=FVG_MIN_ATR)
        bull_fvgs = [f for f in fvgs if f["type"] == "bull" and not f["filled"]]
        if not bull_fvgs:
            return None
        fvg = bull_fvgs[0]
        entry_price = (fvg["top"] + fvg["bot"]) / 2.0

        return {
            "type": "A",
            "direction": "long",
            "entry_price": round(entry_price, 5),
            "fvg_top": fvg["top"],
            "fvg_bot": fvg["bot"],
            "sweep_extreme": sweep_extreme,
            "swept_level": swept_level,
            "pattern_bar": len(m15_df) - 2,
            "session": _session_label(hour_utc),
            "note": f"KZ sweep {swept_level:.2f} + FVG",
        }

    else:  # short
        targets = []
        if asian_high is not None:
            targets.append(asian_high)
        if prev_day_high is not None:
            targets.append(prev_day_high)
        if not targets:
            return None

        sweep_extreme = None
        swept_level   = None
        for lvl in targets:
            ext = high - lvl
            if ext >= SWEEP_MIN_ATR * atr and close < lvl:
                sweep_extreme = high
                swept_level   = lvl
                break
        if sweep_extreme is None:
            return None

        fvg_window = m15_df.iloc[-8:-1]
        if len(fvg_window) < 3:
            return None
        fvgs = detect_fvg(fvg_window, atr, min_size_atr=FVG_MIN_ATR)
        bear_fvgs = [f for f in fvgs if f["type"] == "bear" and not f["filled"]]
        if not bear_fvgs:
            return None
        fvg = bear_fvgs[0]
        entry_price = (fvg["top"] + fvg["bot"]) / 2.0

        return {
            "type": "A",
            "direction": "short",
            "entry_price": round(entry_price, 5),
            "fvg_top": fvg["top"],
            "fvg_bot": fvg["bot"],
            "sweep_extreme": sweep_extreme,
            "swept_level": swept_level,
            "pattern_bar": len(m15_df) - 2,
            "session": _session_label(hour_utc),
            "note": f"KZ sweep {swept_level:.2f} + FVG",
        }


def detect_pattern_b(
    m15_df: pd.DataFrame,
    direction: str,
    atr: float,
) -> Optional[Dict]:
    """
    Pattern B: BOS Retest with Rejection Candle.

    Steps:
      1. Detect a recent M15 BOS in the trade direction (within last 15 bars)
      2. Price pulls back to the BOS level (within 1.5 ATR)
      3. Last completed bar shows rejection (wick ≥ 40% of range on correct side)

    Returns setup dict or None.
    """
    if len(m15_df) < 20 or atr <= 0:
        return None

    # Find BOS in recent bars — scan back up to 15 bars
    # We test multiple slice endpoints to find the most recent BOS
    bos_level = None
    bos_bar   = None

    for back in range(3, min(40, len(m15_df) - 5)):
        test_df = m15_df.iloc[:-back]
        if len(test_df) < 10:
            break
        recent   = test_df.iloc[-min(25, len(test_df)):]
        swings   = find_swings(recent, order=2)
        test_atr = _compute_atr(recent)
        bos      = detect_bos(recent, swings, test_atr)

        if direction == "long" and bos.get("bull") and bos.get("level"):
            bos_level = bos["level"]
            bos_bar   = len(m15_df) - back
            break
        elif direction == "short" and bos.get("bear") and bos.get("level"):
            bos_level = bos["level"]
            bos_bar   = len(m15_df) - back
            break

    if bos_level is None or bos_bar is None:
        return None

    # Check that the retest occurs within BOS_RETEST_MAX bars
    bars_since_bos = (len(m15_df) - 2) - bos_bar
    if bars_since_bos > BOS_RETEST_MAX:
        return None

    # Last completed bar
    bar   = m15_df.iloc[-2]
    high  = float(bar["high"])
    low   = float(bar["low"])
    close = float(bar["close"])
    open_ = float(bar["open"])
    total_range = max(high - low, 1e-8)

    if direction == "long":
        # Price must be near the BOS level (within 1.5 ATR — tighter retest quality filter)
        dist_to_level = abs(low - bos_level)
        if dist_to_level > 1.5 * atr:
            return None
        # Rejection candle: lower wick ≥ REJECT_WICK of range
        lower_wick = min(open_, close) - low
        if lower_wick / total_range < REJECT_WICK:
            return None
        entry_price = close

    else:  # short
        dist_to_level = abs(high - bos_level)
        if dist_to_level > 1.5 * atr:
            return None
        upper_wick = high - max(open_, close)
        if upper_wick / total_range < REJECT_WICK:
            return None
        entry_price = close

    return {
        "type": "B",
        "direction": direction,
        "entry_price": round(entry_price, 5),
        "sweep_extreme": low if direction == "long" else high,
        "bos_level": bos_level,
        "bars_since_bos": bars_since_bos,
        "pattern_bar": len(m15_df) - 2,
        "session": "any",
        "note": f"BOS retest at {bos_level:.2f}",
    }


def pattern_priority(setups: list) -> Optional[Dict]:
    """
    Select the highest priority setup from a list of detected patterns.
    Priority: C > A > B (C has highest win rate, B has most lag).
    """
    for ptype in ("C", "A", "B"):
        for s in setups:
            if s is not None and s.get("type") == ptype:
                return s
    return None


def scan_diagnostic(m15_df: pd.DataFrame, direction: str, atr: float) -> str:
    """
    Return a one-line string explaining why no pattern was found for `direction`.
    Used for DEBUG logging only — never called in the trade path.
    """
    if len(m15_df) < 10 or atr <= 0:
        return "insufficient data"

    bar  = m15_df.iloc[-2]
    high = float(bar["high"]); low = float(bar["low"])
    close= float(bar["close"]); open_= float(bar["open"])
    body = abs(close - open_)
    rng  = max(high - low, 1e-8)
    wick = (min(open_, close) - low) if direction == "long" else (high - max(open_, close))

    reasons = []

    # BOS check
    recent   = m15_df.iloc[-min(30, len(m15_df)-1):-1]
    swings   = find_swings(recent, order=2)
    bos_info = detect_bos(recent, swings, atr)
    if direction == "long":
        if bos_info.get("bull") and bos_info.get("level"):
            dist = abs(low - bos_info["level"])
            reasons.append(f"BOS@{bos_info['level']:.1f} dist={dist:.1f}(max={1.5*atr:.1f})")
        else:
            reasons.append(f"no bull BOS (body={body:.1f} need>{0.4*atr:.1f})")
    else:
        if bos_info.get("bear") and bos_info.get("level"):
            dist = abs(high - bos_info["level"])
            reasons.append(f"BOS@{bos_info['level']:.1f} dist={dist:.1f}(max={1.5*atr:.1f})")
        else:
            reasons.append(f"no bear BOS (body={body:.1f} need>{0.4*atr:.1f})")

    # Wick check
    wick_pct = wick / rng
    reasons.append(f"wick={wick:.1f}({wick_pct:.0%} need>{REJECT_WICK:.0%})")

    # Equal levels check
    eq = detect_equal_levels(swings, atr, tol=EQL_TOL_ATR)
    eql = eq.get("eql"); eqh = eq.get("eqh")
    if direction == "long":
        if eql:
            reasons.append(f"EQL@{eql:.1f} sweep_ext={eql-low:.1f}(need>{SWEEP_MIN_ATR*atr:.1f})")
        else:
            reasons.append("no EQL cluster")
    else:
        if eqh:
            reasons.append(f"EQH@{eqh:.1f} sweep_ext={high-eqh:.1f}(need>{SWEEP_MIN_ATR*atr:.1f})")
        else:
            reasons.append("no EQH cluster")

    return " | ".join(reasons)
