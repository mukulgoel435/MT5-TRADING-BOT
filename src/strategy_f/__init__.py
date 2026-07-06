"""
strategy_f — market structure library used by Strategy H.

Only market_structure.py remains; the full Strategy F runner was removed.
"""

from src.strategy_f.market_structure import (
    find_swings,
    current_trend,
    detect_bos,
    detect_choch,
    detect_liquidity_sweep,
    detect_fvg,
    price_in_fvg,
    detect_order_blocks,
    price_in_ob,
    detect_equal_levels,
    detect_displacement,
    candle_quality_score,
)

__version__ = "1.0.0"
