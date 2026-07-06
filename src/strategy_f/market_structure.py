"""
src/strategy_f/market_structure.py

All market structure detection for Strategy F v2.
Pure functions — no state, no side effects.
Every function takes a pandas DataFrame of OHLCV candles and returns structured dicts.

All candle indices use COMPLETED bars only (never the forming bar).
The forming bar is df.iloc[-1]; completed bars are df.iloc[:-1].
"""

import math
import pandas as pd
import numpy as np
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# SWING HIGHS / LOWS
# ─────────────────────────────────────────────────────────────────────────────

def find_swings(df: pd.DataFrame, order: int = 2) -> dict:
    """
    Find confirmed swing highs and lows using n-bar pivot confirmation.
    order=2 means 2 bars on each side must be lower/higher (confirmed 2 bars after peak).

    Returns:
        {"highs": [(bar_idx, price), ...], "lows": [(bar_idx, price), ...]}
        Ordered oldest→newest. Excludes the last `order` bars (not yet confirmed).
    """
    highs_arr = df["high"].values
    lows_arr  = df["low"].values
    n = len(highs_arr)

    swing_highs = []
    swing_lows  = []

    for i in range(order, n - order):
        if all(highs_arr[i] > highs_arr[i - j] for j in range(1, order + 1)) and \
           all(highs_arr[i] > highs_arr[i + j] for j in range(1, order + 1)):
            swing_highs.append((i, float(highs_arr[i])))

        if all(lows_arr[i]  < lows_arr[i - j]  for j in range(1, order + 1)) and \
           all(lows_arr[i]  < lows_arr[i + j]   for j in range(1, order + 1)):
            swing_lows.append((i, float(lows_arr[i])))

    return {"highs": swing_highs, "lows": swing_lows}


def current_trend(swings: dict) -> str:
    """
    Derive trend from swing sequence.
    UP:   last two lows form Higher Low  AND  last two highs form Higher High.
    DOWN: last two highs form Lower High AND  last two lows  form Lower Low.
    NEUTRAL otherwise.
    """
    highs = [s[1] for s in swings["highs"]]
    lows  = [s[1] for s in swings["lows"]]
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1] > highs[-2]
        hl = lows[-1]  > lows[-2]
        lh = highs[-1] < highs[-2]
        ll = lows[-1]  < lows[-2]
        if hh and hl:
            return "UP"
        if lh and ll:
            return "DOWN"
    return "NEUTRAL"


# ─────────────────────────────────────────────────────────────────────────────
# BREAK OF STRUCTURE (BOS)
# ─────────────────────────────────────────────────────────────────────────────

def detect_bos(df: pd.DataFrame, swings: dict, atr: float) -> dict:
    """
    Break of Structure: last COMPLETED candle's close breaks the most recent confirmed
    swing high (bullish BOS) or swing low (bearish BOS).

    Anti-false-BOS guard: breaking candle body must be ≥ 0.8×ATR.
    Returns:
        {"bull": bool, "bear": bool, "level": float|None, "strength": float}
        strength = body / atr  (> 1.0 = strong displacement)
    """
    if len(df) < 3:
        return {"bull": False, "bear": False, "level": None, "strength": 0.0}

    bar   = df.iloc[-2]          # last completed candle
    close = float(bar["close"])
    open_ = float(bar["open"])
    body  = abs(close - open_)
    strength = body / atr if atr > 0 else 0.0

    result = {"bull": False, "bear": False, "level": None, "strength": round(strength, 2)}

    if body < 0.4 * atr:        # weak candle — not a real BOS (tuned from 0.8)
        return result

    if swings["highs"]:
        last_sh = swings["highs"][-1][1]
        if close > last_sh:
            result["bull"]  = True
            result["level"] = last_sh
            return result          # bull BOS takes priority if both (rare)

    if swings["lows"]:
        last_sl = swings["lows"][-1][1]
        if close < last_sl:
            result["bear"]  = True
            result["level"] = last_sl

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CHANGE OF CHARACTER (CHoCH)
# ─────────────────────────────────────────────────────────────────────────────

def detect_choch(df: pd.DataFrame, swings: dict, trend: str) -> dict:
    """
    Change of Character: price violates the last internal structure level against
    the prevailing trend direction.

    In uptrend (HH, HL): bearish CHoCH when close breaks the last Higher Low.
    In downtrend (LH, LL): bullish CHoCH when close breaks the last Lower High.

    Returns:
        {"bull": bool, "bear": bool}
    """
    if len(df) < 2:
        return {"bull": False, "bear": False}

    close = float(df.iloc[-2]["close"])
    result = {"bull": False, "bear": False}

    if trend == "UP" and len(swings["lows"]) >= 2:
        lows_prices = [s[1] for s in swings["lows"]]
        if lows_prices[-1] > lows_prices[-2]:   # sequence confirmed HL
            if close < lows_prices[-1]:
                result["bear"] = True            # uptrend Higher Low violated
    elif trend == "DOWN" and len(swings["highs"]) >= 2:
        highs_prices = [s[1] for s in swings["highs"]]
        if highs_prices[-1] < highs_prices[-2]: # sequence confirmed LH
            if close > highs_prices[-1]:
                result["bull"] = True            # downtrend Lower High violated

    return result


# ─────────────────────────────────────────────────────────────────────────────
# LIQUIDITY SWEEPS
# ─────────────────────────────────────────────────────────────────────────────

def detect_liquidity_sweep(df: pd.DataFrame, swings: dict, atr: float) -> dict:
    """
    Liquidity sweep: a completed candle's wick extends beyond a swing level
    but the close is back inside — indicating a stop hunt.

    Quality thresholds:
      - Wick extension > 0.3×ATR beyond the swing level.
      - Close retraces back inside the swing level (the real signal).

    Returns:
        {"bull": bool, "bear": bool, "wick": float|None}
        bull sweep = swept LOWS (stop hunt of buy stops → price reversed up)
        bear sweep = swept HIGHS (stop hunt of sell stops → price reversed down)
    """
    result = {"bull": False, "bear": False, "wick": None}
    if len(df) < 3:
        return result

    bar   = df.iloc[-2]
    high  = float(bar["high"])
    low   = float(bar["low"])
    close = float(bar["close"])

    if swings["highs"]:
        last_sh = swings["highs"][-1][1]
        ext = high - last_sh
        if ext > 0.3 * atr and close < last_sh:
            result["bear"] = True
            result["wick"] = high

    if swings["lows"] and not result["bear"]:   # only check lows if no bear sweep
        last_sl = swings["lows"][-1][1]
        ext = last_sl - low
        if ext > 0.3 * atr and close > last_sl:
            result["bull"] = True
            result["wick"] = low

    return result


# ─────────────────────────────────────────────────────────────────────────────
# FAIR VALUE GAP (FVG)
# ─────────────────────────────────────────────────────────────────────────────

def detect_fvg(df: pd.DataFrame, atr: float, min_size_atr: float = 0.25) -> list:
    """
    Fair Value Gap: 3-candle imbalance where candle[i] and candle[i-2] do not overlap.
    Represents price levels where no two-way price discovery occurred.

    Minimum FVG size = min_size_atr × ATR (filters noise).
    Returns list of FVG dicts, newest first:
        [{"type": "bull"|"bear", "top": float, "bot": float, "bar": int, "filled": bool}, ...]
    """
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    fvgs   = []
    n      = len(highs)

    for i in range(2, n - 1):   # exclude the forming bar
        # Bullish FVG: gap up — lows[i] > highs[i-2]
        gap = float(lows[i]) - float(highs[i - 2])
        if gap > min_size_atr * atr:
            filled = any(float(lows[j]) <= float(highs[i - 2]) + gap * 0.5
                         for j in range(i + 1, n - 1))
            fvgs.append({"type": "bull", "top": float(lows[i]),
                         "bot": float(highs[i - 2]), "bar": i, "filled": filled})

        # Bearish FVG: gap down — highs[i] < lows[i-2]
        gap = float(lows[i - 2]) - float(highs[i])
        if gap > min_size_atr * atr:
            filled = any(float(highs[j]) >= float(lows[i - 2]) - gap * 0.5
                         for j in range(i + 1, n - 1))
            fvgs.append({"type": "bear", "top": float(lows[i - 2]),
                         "bot": float(highs[i]), "bar": i, "filled": filled})

    return list(reversed(fvgs))   # newest first


def price_in_fvg(price: float, fvgs: list, side: str) -> tuple:
    """
    Return (True, fvg_dict) if price is inside an unmitigated FVG of matching type.
    Returns (False, None) otherwise.
    Only checks the 8 most recent FVGs.
    """
    for fvg in fvgs[:8]:
        if not fvg.get("filled") and fvg["type"] == side:
            if fvg["bot"] <= price <= fvg["top"]:
                return True, fvg
    return False, None


# ─────────────────────────────────────────────────────────────────────────────
# ORDER BLOCKS (OB)
# ─────────────────────────────────────────────────────────────────────────────

def detect_order_blocks(df: pd.DataFrame, atr: float,
                        impulse_mult: float = 1.5,
                        lookback: int = 30) -> list:
    """
    Order Block: the last opposite-direction candle before an impulsive move.
    The impulse must be > impulse_mult × ATR in the move direction within 4 bars.

    OB is invalidated when price closes through it.
    Returns newest-first list:
        [{"type": "bull"|"bear", "top": float, "bot": float, "bar": int, "valid": bool}, ...]
    """
    obs    = []
    closes = df["close"].values
    opens  = df["open"].values
    highs  = df["high"].values
    lows   = df["low"].values
    n      = len(closes)
    start  = max(1, n - lookback - 4)

    for i in range(start, n - 4):
        # Bullish OB: bearish candle followed by bullish impulse
        if float(closes[i]) < float(opens[i]):
            move = float(closes[min(i + 4, n - 1)]) - float(closes[i])
            if move > impulse_mult * atr:
                top  = max(float(opens[i]), float(closes[i]))
                bot  = min(float(opens[i]), float(closes[i]))
                # Valid unless price has since closed below bot
                subsequent_low = min(float(lows[j]) for j in range(i + 1, n - 1))
                valid = subsequent_low > bot
                obs.append({"type": "bull", "top": top, "bot": bot,
                            "bar": i, "valid": valid})

        # Bearish OB: bullish candle followed by bearish impulse
        if float(closes[i]) > float(opens[i]):
            move = float(closes[i]) - float(closes[min(i + 4, n - 1)])
            if move > impulse_mult * atr:
                top  = max(float(opens[i]), float(closes[i]))
                bot  = min(float(opens[i]), float(closes[i]))
                subsequent_high = max(float(highs[j]) for j in range(i + 1, n - 1))
                valid = subsequent_high < top
                obs.append({"type": "bear", "top": top, "bot": bot,
                            "bar": i, "valid": valid})

    return list(reversed(obs))


def price_in_ob(price: float, obs: list, side: str) -> tuple:
    """Return (True, ob_dict) if price is inside a valid OB of matching type."""
    for ob in obs[:6]:
        if ob.get("valid") and ob["type"] == side:
            if ob["bot"] <= price <= ob["top"]:
                return True, ob
    return False, None


# ─────────────────────────────────────────────────────────────────────────────
# EQUAL HIGHS / LOWS (EQH / EQL)
# ─────────────────────────────────────────────────────────────────────────────

def detect_equal_levels(swings: dict, atr: float, tol: float = 0.25) -> dict:
    """
    Equal Highs (EQH) / Equal Lows (EQL): two swing levels within tol×ATR of each other.
    These are liquidity pools — price will typically sweep them for stops.

    Returns:
        {"eqh": float|None, "eql": float|None}
        Value is the average price of the equal level cluster.
    """
    result = {"eqh": None, "eql": None}
    if atr <= 0:
        return result

    highs = [s[1] for s in swings["highs"]]
    for i in range(len(highs) - 1):
        if abs(highs[-1] - highs[i]) < tol * atr:
            result["eqh"] = round((highs[-1] + highs[i]) / 2, 5)
            break

    lows = [s[1] for s in swings["lows"]]
    for i in range(len(lows) - 1):
        if abs(lows[-1] - lows[i]) < tol * atr:
            result["eql"] = round((lows[-1] + lows[i]) / 2, 5)
            break

    return result


# ─────────────────────────────────────────────────────────────────────────────
# DISPLACEMENT CANDLE
# ─────────────────────────────────────────────────────────────────────────────

def detect_displacement(df: pd.DataFrame, atr: float,
                        mult: float = 1.5, lookback: int = 5) -> dict:
    """
    Displacement: a candle with body > mult×ATR — signals institutional participation.
    Searches the last `lookback` completed bars for the strongest displacement.

    Returns:
        {"present": bool, "direction": "bull"|"bear"|None, "bar": int|None, "strength": float}
        strength = body / atr
    """
    result = {"present": False, "direction": None, "bar": None, "strength": 0.0}
    if len(df) < 3:
        return result

    closes = df["close"].values
    opens  = df["open"].values
    n      = len(closes)

    for i in range(max(1, n - lookback - 1), n - 1):  # exclude forming bar
        body = abs(float(closes[i]) - float(opens[i]))
        if body > mult * atr:
            direction = "bull" if float(closes[i]) > float(opens[i]) else "bear"
            strength  = body / atr if atr > 0 else 0.0
            if strength > result["strength"]:
                result = {"present": True, "direction": direction,
                          "bar": i, "strength": round(strength, 2)}

    return result


# ─────────────────────────────────────────────────────────────────────────────
# CANDLE QUALITY SCORE  (0–100)
# ─────────────────────────────────────────────────────────────────────────────

def candle_quality_score(df: pd.DataFrame, atr: float) -> float:
    """
    Score the last COMPLETED M1 candle quality (0–100).
    Higher score = cleaner, more conviction candle.

    Scoring dimensions:
      Body/ATR ratio      (0–30): bigger body = more conviction
      Total range/ATR     (0–20): active candle vs. baseline
      Wick ratio          (0–20): small wicks = clean directional move
      Close location      (0–15): closes near high (bull) or low (bear) = strength
      Momentum vs prev    (0–15): body larger than previous candle = acceleration
    """
    if len(df) < 3 or atr <= 0:
        return 0.0

    bar   = df.iloc[-2]     # last completed
    prev  = df.iloc[-3]
    high  = float(bar["high"])
    low   = float(bar["low"])
    close = float(bar["close"])
    open_ = float(bar["open"])

    body        = abs(close - open_)
    total_range = max(high - low, 1e-8)
    upper_wick  = high - max(close, open_)
    lower_wick  = min(close, open_) - low

    score = 0.0

    # Body/ATR (0–30)
    score += min(30.0, (body / atr) * 22.0)

    # Range/ATR (0–20)
    score += min(20.0, (total_range / atr) * 14.0)

    # Wick ratio (0–20): less wick = better
    wick_frac = (upper_wick + lower_wick) / total_range
    score += max(0.0, 20.0 * (1.0 - wick_frac))

    # Close location (0–15)
    if close > open_:
        close_loc = (close - low) / total_range
    else:
        close_loc = (high - close) / total_range
    score += min(15.0, close_loc * 15.0)

    # Momentum vs previous bar (0–15)
    prev_body = abs(float(prev["close"]) - float(prev["open"]))
    if prev_body > 0:
        accel = (body - prev_body) / prev_body
        score += min(15.0, max(0.0, accel * 10.0))

    return min(100.0, round(score, 1))
