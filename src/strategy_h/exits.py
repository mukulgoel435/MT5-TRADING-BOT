"""
src/strategy_h/exits.py — Exit management for Strategy H.

Exit hierarchy (checked in order):
  1. SL hit                  → full close, immediate
  2. TP1 hit (1:1)           → 50% close, SL → breakeven, begin trail
  3. Trailing SL             → ATR-based trail from best price seen (after TP1)
  4. Time stop (20 bars)     → full close if TP1 not yet reached
  5. Regime exit             → close remaining 50% if D1 structure flips

All functions are pure — they return instructions, not side effects.
"""

import numpy as np
import pandas as pd
from typing import Optional, Tuple

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from strategy_f.market_structure import find_swings

# ── Constants (mirror backtest_h.py values) ──────────────────────────────────
SL_MIN_ATR   = 1.5    # minimum SL distance in ATR multiples
SL_MAX_ATR   = 3.0    # maximum SL distance in ATR multiples
SL_BUF_ATR   = 0.3    # buffer beyond structural extreme
TP1_RR       = 1.0    # TP1 at 1:1 RR
TIME_STOP    = 48     # bars until time stop fires (12 hours on M15)
TRAIL_ATR    = 1.5    # ATR multiple: trail SL stays this far from best price seen
TRAIL_ORDER  = 2      # n-bar swing pivot order for secondary swing-trail
TRAIL_BUF    = 0.2    # ATR buffer below/above swing for swing trail


def compute_sl(
    entry_price: float,
    direction: str,
    atr: float,
    sweep_extreme: Optional[float] = None,
) -> float:
    """
    Compute initial stop loss level.
    Structural: sweep extreme + SL_BUF_ATR buffer.
    Floor:      entry ± SL_MIN_ATR × ATR (prevents micro-SL).
    Ceiling:    entry ± SL_MAX_ATR × ATR (skips overly-wide setups in caller).

    Returns the raw SL price (caller validates that distance ≤ SL_MAX_ATR × ATR).
    """
    floor_dist = SL_MIN_ATR * atr

    if direction == "long":
        structural = (sweep_extreme - SL_BUF_ATR * atr) if sweep_extreme else (entry_price - floor_dist)
        sl = min(structural, entry_price - floor_dist)   # take the more conservative (wider) of the two
    else:
        structural = (sweep_extreme + SL_BUF_ATR * atr) if sweep_extreme else (entry_price + floor_dist)
        sl = max(structural, entry_price + floor_dist)

    return round(sl, 5)


def compute_tp1(entry_price: float, sl_price: float, direction: str) -> float:
    """TP1 at exactly 1:1 risk:reward."""
    sl_dist = abs(entry_price - sl_price)
    if direction == "long":
        return round(entry_price + sl_dist * TP1_RR, 5)
    return round(entry_price - sl_dist * TP1_RR, 5)


def check_sl_hit(bar_low: float, bar_high: float, sl: float, direction: str) -> bool:
    """Return True if the bar has traded through the stop loss level."""
    if direction == "long":
        return bar_low <= sl
    return bar_high >= sl


def check_tp1_hit(bar_low: float, bar_high: float, tp1: float, direction: str) -> bool:
    """Return True if the bar has reached TP1."""
    if direction == "long":
        return bar_high >= tp1
    return bar_low <= tp1


def check_time_stop(bars_held: int, tp1_reached: bool) -> bool:
    """Fire time stop only when TP1 has NOT been reached."""
    return (not tp1_reached) and (bars_held >= TIME_STOP)


def update_trail_sl(
    position: dict,
    m15_df: pd.DataFrame,
    atr: float,
    current_price: float = None,
) -> float:
    """
    Compute updated trailing SL for the remaining 50% after TP1.

    Primary:   ATR-based trail — SL = best_price - TRAIL_ATR * atr (for long).
               `best_price` is the highest price seen since entry (tracked in position dict).
    Secondary: Swing-based trail (M15 swing pivots) — used if it gives a tighter SL.

    The trail only ever moves in the direction of profit (never widens).
    Caller must update position["best_price"] = current_price after this call.

    Returns the new SL price (may equal current_sl if no improvement possible yet).
    """
    direction  = position["direction"]
    current_sl = position["current_sl"]
    entry      = position["entry_price"]

    # ── ATR-based trailing (primary) ─────────────────────────────────────────
    best = position.get("best_price", entry)
    if current_price is not None:
        best = max(best, current_price) if direction == "long" else min(best, current_price)

    if direction == "long":
        atr_sl  = round(best - TRAIL_ATR * atr, 5)
        new_sl  = max(current_sl, atr_sl)   # only move upward (toward profit)
    else:
        atr_sl  = round(best + TRAIL_ATR * atr, 5)
        new_sl  = min(current_sl, atr_sl)   # only move downward

    # ── Swing-based trailing (secondary — use if tighter than ATR trail) ─────
    if len(m15_df) >= TRAIL_ORDER * 2 + 3:
        lookback_start = max(0, len(m15_df) - 30)
        recent = m15_df.iloc[lookback_start:]
        if len(recent) >= TRAIL_ORDER * 2 + 2:
            try:
                swings = find_swings(recent, order=TRAIL_ORDER)
                if direction == "long":
                    lows = [s[1] for s in swings.get("lows", [])]
                    if lows:
                        swing_sl = round(lows[-1] - TRAIL_BUF * atr, 5)
                        new_sl = max(new_sl, swing_sl)   # take whichever is higher (tighter)
                else:
                    highs = [s[1] for s in swings.get("highs", [])]
                    if highs:
                        swing_sl = round(highs[-1] + TRAIL_BUF * atr, 5)
                        new_sl = min(new_sl, swing_sl)
            except Exception:
                pass   # swing detection failed — ATR trail is sufficient

    return new_sl


def check_regime_exit(
    position: dict,
    d1_regime: str,
    h4_direction: str,
) -> bool:
    """
    Exit remaining position if D1 + H4 structure has fully flipped against us.
    Only fires AFTER TP1 has been reached (protecting the profitable portion).
    """
    if not position.get("tp1_reached", False):
        return False

    direction = position["direction"]

    if direction == "long":
        # Both D1 bear AND H4 bear = confirmed flip against long
        return (d1_regime == "bear") and (h4_direction == "bear")
    else:
        return (d1_regime == "bull") and (h4_direction == "bull")


def sl_distance_valid(entry_price: float, sl_price: float, atr: float) -> bool:
    """Return True if SL distance is within the allowed [SL_MIN_ATR, SL_MAX_ATR] range."""
    dist = abs(entry_price - sl_price)
    return (SL_MIN_ATR * atr) <= dist <= (SL_MAX_ATR * atr)
