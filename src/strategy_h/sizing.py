"""
src/strategy_h/sizing.py — Position sizing for Strategy H.
Fixed fractional risk model: 1% per trade (or 0.5% in drawdown protection mode).
No grid, no averaging, no scaling into positions.
"""

PIP_SIZE = 0.1
PIP_VAL  = 10.0   # $ per pip per lot for XAUUSD
LOT_MIN  = 0.01
LOT_MAX  = 0.10


def compute_lot(balance: float, sl_distance: float, risk_pct: float = 0.01) -> float:
    """
    Compute lot size from fixed fractional risk.

    risk_usd = balance × risk_pct
    pip_distance = sl_distance / PIP_SIZE
    lot = risk_usd / (pip_distance × PIP_VAL)

    Clamped to [LOT_MIN, LOT_MAX].
    """
    if sl_distance <= 0 or balance <= 0:
        return LOT_MIN
    risk_usd  = balance * risk_pct
    pip_dist  = sl_distance / PIP_SIZE
    if pip_dist <= 0:
        return LOT_MIN
    lot = risk_usd / (pip_dist * PIP_VAL)
    return max(LOT_MIN, min(LOT_MAX, round(lot, 2)))


def compute_portfolio_heat(positions: list, balance: float) -> float:
    """
    Return total at-risk % across all open positions whose SL is not yet
    at breakeven. A position at breakeven contributes 0 heat.
    positions: list of dicts with keys: direction, entry_price, current_sl, lot
    """
    heat = 0.0
    for p in positions:
        entry = p.get("entry_price", 0.0)
        sl    = p.get("current_sl", entry)
        lot   = p.get("lot", 0.0)
        direction = p.get("direction", "long")
        if direction == "long":
            risk_pts = max(0.0, entry - sl)
        else:
            risk_pts = max(0.0, sl - entry)
        if risk_pts <= 0:
            continue
        risk_usd = (risk_pts / PIP_SIZE) * PIP_VAL * lot
        heat += risk_usd / balance if balance > 0 else 0.0
    return heat
