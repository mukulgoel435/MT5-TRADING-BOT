"""
src/strategy_h/regime.py — Multi-timeframe regime engine for Strategy H.

Three-layer system:
  Layer 1 (D1):   Macro trend via swing structure + EMA20/50 alignment
  Layer 2 (H4):   Intermediate direction via H4 swing structure
  Layer 3 (bias): Daily bias combining D1 + H4 + premium/discount context

All functions are pure — no state, no side effects.
Input DataFrames must contain completed (closed) bars only.
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple

# Import existing market structure functions
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategy_f.market_structure import find_swings, current_trend

# ── Regime constants ─────────────────────────────────────────────────────────
D1_BULL     = "bull"
D1_BEAR     = "bear"
D1_RANGE    = "range"
D1_CHOP     = "chop"       # expanding range, no direction — no new entries
D1_COMPRESS = "compress"   # compressing range — range entries only

H4_BULL = "bull"
H4_BEAR = "bear"
H4_NONE = "none"

BIAS_LONG   = "long"         # long only
BIAS_SHORT  = "short"        # short only
BIAS_LONG_P = "long_pref"    # long preferred, shorts need higher quality
BIAS_SHORT_P = "short_pref"  # short preferred, longs need higher quality
BIAS_NEUTRAL = "neutral"     # both directions equal quality


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_d1_regime(d1_df: pd.DataFrame) -> str:
    """
    Determine D1 macro regime from daily OHLCV data.

    Logic:
      - Derive swing structure (HH/HL = UP, LH/LL = DOWN, else NEUTRAL)
      - Cross-check with EMA20 > EMA50 alignment
      - D1_CHOP when range is expanding (HH + LL simultaneously)
      - D1_COMPRESS when range is compressing (LH + HL simultaneously)
      - D1_BULL / D1_BEAR / D1_RANGE otherwise

    Returns one of: D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS
    """
    if len(d1_df) < 10:
        return D1_RANGE

    # Use at most last 30 D1 bars for swing detection
    recent = d1_df.iloc[-min(30, len(d1_df)):]
    swings = find_swings(recent, order=2)
    trend  = current_trend(swings)

    # EMA alignment on full D1 history for stability
    closes   = d1_df["close"]
    ema20    = _ema(closes, 20).iloc[-1]
    ema50    = _ema(closes, 50).iloc[-1]
    last_c   = float(closes.iloc[-1])

    ema_bull = (ema20 > ema50) and (last_c > ema20)
    ema_bear = (ema20 < ema50) and (last_c < ema20)

    shs = [s[1] for s in swings["highs"]]
    sls = [s[1] for s in swings["lows"]]

    # Expanding range — no clear direction, dangerous for entries
    if len(shs) >= 2 and len(sls) >= 2:
        hh = shs[-1] > shs[-2]
        ll = sls[-1] < sls[-2]
        lh = shs[-1] < shs[-2]
        hl = sls[-1] > sls[-2]
        if hh and ll:
            return D1_CHOP
        if lh and hl:
            return D1_COMPRESS

    # Clear trending structure
    if trend == "UP" or ema_bull:
        return D1_BULL
    if trend == "DOWN" or ema_bear:
        return D1_BEAR

    return D1_RANGE


def compute_h4_direction(h4_df: pd.DataFrame) -> str:
    """
    Determine H4 intermediate direction from H4 OHLCV data.
    Uses swing structure only (HH/HL = bull, LH/LL = bear).
    Returns H4_BULL, H4_BEAR, or H4_NONE.
    """
    if len(h4_df) < 10:
        return H4_NONE

    lookback = min(50, len(h4_df))
    recent   = h4_df.iloc[-lookback:]
    swings   = find_swings(recent, order=2)
    trend    = current_trend(swings)

    if trend == "UP":
        return H4_BULL
    if trend == "DOWN":
        return H4_BEAR
    return H4_NONE


def compute_premium_discount(price: float, d1_range_h: float, d1_range_l: float) -> float:
    """
    Return price's position within the D1 range as 0.0–1.0.
    0.0 = extreme discount (bottom of range), 1.0 = extreme premium (top).
    Returns 0.5 (neutral) if range is invalid.
    """
    span = d1_range_h - d1_range_l
    if span <= 0:
        return 0.5
    pd_val = (price - d1_range_l) / span
    return max(0.0, min(1.0, pd_val))


def compute_daily_bias(
    d1_regime:      str,
    h4_direction:   str,
    price:          float,
    d1_range_h:     float,
    d1_range_l:     float,
    prev_day_h:     Optional[float],
    prev_day_l:     Optional[float],
    prev_day_close: Optional[float],
) -> Tuple[str, int]:
    """
    Compute daily directional bias from all available context.

    Scoring:
      D1 regime (up to ±40)
      H4 direction (up to ±30)
      Premium/Discount position (±15)
      Prior day close position (±15)

    Returns (bias_str, score) where score is the winning side's score (0–100).
    """
    score_bull = 0
    score_bear = 0

    # D1 regime
    if d1_regime == D1_BULL:
        score_bull += 40
    elif d1_regime == D1_BEAR:
        score_bear += 40
    elif d1_regime == D1_CHOP:
        # Expanding range — strongly penalise both sides
        score_bull -= 20
        score_bear -= 20

    # H4 direction
    if h4_direction == H4_BULL:
        score_bull += 30
    elif h4_direction == H4_BEAR:
        score_bear += 30

    # Premium/Discount context
    if d1_range_h and d1_range_l and d1_range_h > d1_range_l:
        pd_val = compute_premium_discount(price, d1_range_h, d1_range_l)
        if pd_val > 0.75:
            score_bear += 15   # price in premium → favour sells
        elif pd_val < 0.25:
            score_bull += 15   # price in discount → favour buys

    # Prior day close position (momentum continuation)
    if prev_day_h and prev_day_l and prev_day_close and (prev_day_h > prev_day_l):
        pdc = (prev_day_close - prev_day_l) / (prev_day_h - prev_day_l)
        if pdc > 0.75:
            score_bull += 15
        elif pdc < 0.25:
            score_bear += 15

    # Determine bias
    score_bull = max(0, score_bull)
    score_bear = max(0, score_bear)

    if score_bull >= 70:
        return BIAS_LONG,   score_bull
    if score_bear >= 70:
        return BIAS_SHORT,  score_bear
    if score_bull > score_bear + 20:
        return BIAS_LONG_P, score_bull
    if score_bear > score_bull + 20:
        return BIAS_SHORT_P, score_bear
    return BIAS_NEUTRAL, max(score_bull, score_bear)


def bias_allows_direction(bias: str, direction: str) -> bool:
    """
    Return True if the given daily bias permits entering in `direction`.
    NEUTRAL allows both directions (no structural preference either way).
    """
    if direction == "long":
        return bias in (BIAS_LONG, BIAS_LONG_P, BIAS_NEUTRAL)
    else:
        return bias in (BIAS_SHORT, BIAS_SHORT_P, BIAS_NEUTRAL)


def entry_direction_from_regime(d1_regime: str, h4_direction: str) -> Optional[str]:
    """
    Return the highest-confidence direction given regime alignment,
    or None if regime is CHOP (no trading allowed).
    """
    if d1_regime == D1_CHOP:
        return None
    if d1_regime == D1_BULL and h4_direction in (H4_BULL, H4_NONE):
        return "long"
    if d1_regime == D1_BEAR and h4_direction in (H4_BEAR, H4_NONE):
        return "short"
    # Mixed signals — both directions available at lower quality
    return "both"
