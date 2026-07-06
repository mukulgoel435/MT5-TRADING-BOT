"""
src/strategy_h/analytics.py — SQLite trade logging for Strategy H.
Schema mirrors g2_analytics for dashboard compatibility where possible.
"""

import os
import sqlite3
from typing import Optional


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "h_analytics.db")


def init_db(db_path: str = DB_PATH):
    """Create trades table if it does not exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            open_time       TEXT,
            close_time      TEXT,
            direction       TEXT,
            pattern         TEXT,
            entry_price     REAL,
            sl_price        REAL,
            tp1_price       REAL,
            exit_price      REAL,
            lot             REAL,
            sl_distance     REAL,
            pnl             REAL,
            pnl_r           REAL,
            result          TEXT,
            exit_reason     TEXT,
            d1_regime       TEXT,
            h4_direction    TEXT,
            daily_bias      TEXT,
            session         TEXT,
            atr_at_entry    REAL,
            bars_held       INTEGER,
            tp1_reached     INTEGER,
            balance_before  REAL,
            balance_after   REAL
        )
    """)
    con.commit()
    con.close()


def log_trade(trade: dict, db_path: str = DB_PATH):
    """Insert one closed trade record."""
    init_db(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO trades (
            open_time, close_time, direction, pattern,
            entry_price, sl_price, tp1_price, exit_price, lot, sl_distance,
            pnl, pnl_r, result, exit_reason,
            d1_regime, h4_direction, daily_bias, session, atr_at_entry,
            bars_held, tp1_reached, balance_before, balance_after
        ) VALUES (
            :open_time, :close_time, :direction, :pattern,
            :entry_price, :sl_price, :tp1_price, :exit_price, :lot, :sl_distance,
            :pnl, :pnl_r, :result, :exit_reason,
            :d1_regime, :h4_direction, :daily_bias, :session, :atr_at_entry,
            :bars_held, :tp1_reached, :balance_before, :balance_after
        )
    """, trade)
    con.commit()
    con.close()


def compute_metrics(db_path: str = DB_PATH) -> dict:
    """Compute summary statistics from logged trades."""
    if not os.path.exists(db_path):
        return {}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM trades ORDER BY open_time").fetchall()
    con.close()

    if not rows:
        return {}

    trades = [dict(r) for r in rows]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total  = len(trades)

    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf         = round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0
    net_pnl    = round(gross_win - gross_loss, 2)
    wr         = round(len(wins) / total * 100, 1) if total > 0 else 0.0

    avg_win    = round(gross_win / len(wins), 2)   if wins   else 0.0
    avg_loss   = round(gross_loss / len(losses), 2) if losses else 0.0
    exp_r      = round(sum(t.get("pnl_r", 0) for t in trades) / total, 3) if total > 0 else 0.0

    time_stops = sum(1 for t in trades if t.get("exit_reason") == "TIME_STOP")
    tp1_hits   = sum(1 for t in trades if t.get("tp1_reached", 0))

    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "win_rate": wr, "profit_factor": pf,
        "net_pnl": net_pnl, "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy_r": exp_r,
        "time_stops": time_stops, "tp1_hits": tp1_hits,
        "tp1_pct": round(tp1_hits / total * 100, 1) if total > 0 else 0.0,
        "time_stop_pct": round(time_stops / total * 100, 1) if total > 0 else 0.0,
    }
