"""
Strategy H — Institutional Session Flow (ISF)
XAUUSD M15 trend-aligned pullback strategy.

Core thesis: gold exploits session-sequenced institutional order flow.
London and NY kill zones create predictable sweep-and-reverse patterns
at session extremes. Entries align with D1 bias, targeting liquidity
events (sweeps, FVGs, BOS retests) for structural entries.

Entry patterns:
  A: Kill Zone Sweep + FVG retrace (highest quality)
  B: Break of Structure retest with rejection candle
  C: Equal High/Low sweep (highest win rate)

Exit: TP1 at 1:1 (50% close) + swing-trail remaining 50%
Risk: 1% per trade, fixed, no grid, no averaging
"""

from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_BULL, H4_BEAR, H4_NONE,
    BIAS_LONG, BIAS_SHORT, BIAS_LONG_P, BIAS_SHORT_P, BIAS_NEUTRAL,
    bias_allows_direction,
)
from src.strategy_h.patterns import (
    detect_pattern_c, detect_pattern_a, detect_pattern_b,
)
from src.strategy_h.exits import (
    check_sl_hit, check_tp1_hit, check_time_stop,
    update_trail_sl, compute_tp1, compute_sl,
)
from src.strategy_h.sizing import compute_lot, LOT_MIN, LOT_MAX
from src.strategy_h.analytics import init_db, log_trade, compute_metrics

__version__ = "1.0.0"
