#!/usr/bin/env python3
"""
scripts/run_live_h.py — Strategy H Institutional Session Flow
=============================================================
Live M15 XAUUSD trader. Polls MT5 every 30s, acts on completed M15 bars.

Usage (DEMO accounts only):
    py -3 scripts/run_live_h.py

Writes:
  data/h_trades.csv               — all closed trades (CSV, append)
  data/h_live_status.json         — current bot state (for dashboard)
  data/h_open_state.json          — open position (survives restart)
  data/logs/h_YYYY-MM-DD.log      — detailed per-bar decision log
"""

import sys, os, time, csv, json, math, logging
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
import MetaTrader5 as mt5

from src.mt5_connector import MT5Connector
from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_BULL, H4_BEAR, H4_NONE,
    BIAS_NEUTRAL,
)
from src.strategy_h.patterns import (
    detect_pattern_c, detect_pattern_a, detect_pattern_b, pattern_priority,
    scan_diagnostic,
)
from src.strategy_h.exits import (
    compute_sl, compute_tp1, check_sl_hit, check_tp1_hit,
    check_time_stop, update_trail_sl, check_regime_exit, sl_distance_valid,
)
from src.strategy_h.sizing import compute_lot

# ── Constants ─────────────────────────────────────────────────────────────────
SYMBOL      = "XAUUSD"
TIMEFRAME   = "M15"
MAGIC       = 202410
LOOP_SLEEP  = 30

BASE     = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR  = os.path.join(DATA_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

TRADES_CSV  = os.path.join(DATA_DIR, "h_trades.csv")
STATUS_JSON = os.path.join(DATA_DIR, "h_live_status.json")
STATE_JSON  = os.path.join(DATA_DIR, "h_open_state.json")

# Strategy parameters
RISK_PCT         = getattr(config, "H_RISK_PCT",           0.01)
RISK_PCT_REDUCED = getattr(config, "H_RISK_PCT_REDUCED",   0.005)
SLIPPAGE         = getattr(config, "H_SLIPPAGE",           0.05)
SPREAD_MAX       = getattr(config, "H_SPREAD_MAX",         2.5)
DAILY_LOSS_LIMIT = getattr(config, "H_DAILY_LOSS_LIMIT",   0.02)
CONSEC_REDUCE    = getattr(config, "H_CONSEC_REDUCE",      3)
CONSEC_HALT      = getattr(config, "H_CONSEC_HALT",        6)
HALT_COOLDOWN    = getattr(config, "H_HALT_COOLDOWN_BARS", 96)
TIME_STOP_BARS   = getattr(config, "H_TIME_STOP_BARS",     20)
PATTERN_A_EXPIRE = getattr(config, "H_PATTERN_A_EXPIRE",   4)
ATR_PERIOD       = getattr(config, "H_ATR_PERIOD",         14)
ATR_AVG_PERIOD   = getattr(config, "H_ATR_AVG_PERIOD",     20)

PIP_SIZE = 0.1
PIP_VAL  = 10.0

TRADE_FIELDS = [
    "open_time", "close_time", "direction", "pattern", "session",
    "entry_price", "sl", "tp1", "exit_price",
    "lot", "sl_distance", "pnl", "pnl_r", "result", "exit_reason",
    "d1_regime", "h4_dir", "daily_bias", "atr_at_entry",
    "bars_held", "tp1_reached", "tp1_pnl",
    "balance_before", "balance_after",
    "best_price_seen", "trail_moves",
]


# ── Logging setup ─────────────────────────────────────────────────────────────

def _make_logger() -> logging.Logger:
    logger = logging.getLogger("strategy_h")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    today = datetime.utcnow().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"h_{today}.log")

    # File handler — 20 MB max, keep 30 files
    fh = RotatingFileHandler(log_path, maxBytes=20 * 1024 * 1024, backupCount=30,
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s UTC | %(levelname)-5s | %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))

    # Console handler — INFO and above only
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("  %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


LOG = _make_logger()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, p: int = 14) -> float:
    if len(df) < 2:
        return 1.0
    import numpy as np
    h = df["high"].values[-p - 2:]
    l = df["low"].values[-p - 2:]
    c = df["close"].values[-p - 2:]
    n = len(h)
    if n < 2:
        return max(float(h[-1] - l[-1]), 1e-9)
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    use = tr[-min(p, len(tr)):]
    return float(use.mean()) if len(use) > 0 else 1.0


def _pnl_r(direction: str, entry: float, sl: float, exit_p: float) -> float:
    sl_dist = abs(entry - sl)
    if sl_dist == 0:
        return 0.0
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / sl_dist, 3)


def _pnl_usd(direction: str, entry: float, exit_p: float, lot: float) -> float:
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / PIP_SIZE * PIP_VAL * lot, 2)


def write_status(data: dict):
    try:
        tmp = STATUS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, STATUS_JSON)
    except Exception as e:
        LOG.error(f"STATUS write error: {e}")


def load_state() -> dict:
    if os.path.exists(STATE_JSON):
        try:
            with open(STATE_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict):
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def append_trade(row: dict):
    new_file = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(row)


def _log_trade(pos: dict, exit_price: float, exit_reason: str,
               pnl_total: float, bars_held: int, result_str: str,
               balance_after: float):
    sl_dist = pos.get("sl_distance", abs(pos["entry_price"] - pos["sl"]))
    pnl_r   = _pnl_r(pos["direction"], pos["entry_price"], pos["sl"], exit_price)
    row = {
        "open_time":       pos.get("entry_time", ""),
        "close_time":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "direction":       pos["direction"],
        "pattern":         pos.get("pattern", "?"),
        "session":         pos.get("session", "?"),
        "entry_price":     round(pos["entry_price"], 5),
        "sl":              round(pos["sl"], 5),
        "tp1":             round(pos["tp1"], 5),
        "exit_price":      exit_price,
        "lot":             pos["lot"],
        "sl_distance":     round(sl_dist, 5),
        "pnl":             pnl_total,
        "pnl_r":           pnl_r,
        "result":          result_str,
        "exit_reason":     exit_reason,
        "d1_regime":       pos.get("d1_regime", ""),
        "h4_dir":          pos.get("h4_dir", ""),
        "daily_bias":      pos.get("daily_bias", ""),
        "atr_at_entry":    round(pos.get("atr_at_entry", 0), 4),
        "bars_held":       bars_held,
        "tp1_reached":     pos.get("tp1_reached", False),
        "tp1_pnl":         round(pos.get("tp1_pnl", 0.0), 2),
        "balance_before":  round(pos.get("balance_before", 0), 2),
        "balance_after":   round(balance_after, 2),
        "best_price_seen": round(pos.get("best_price", pos["entry_price"]), 5),
        "trail_moves":     pos.get("trail_moves", 0),
    }
    append_trade(row)

    LOG.info(
        f"TRADE CLOSED | {pos['direction'].upper()} Pattern-{pos.get('pattern','?')} | "
        f"entry={pos['entry_price']:.2f} exit={exit_price:.2f} | "
        f"PnL={pnl_total:+.2f} ({pnl_r:+.2f}R) | "
        f"bars={bars_held} tp1={'Y' if pos.get('tp1_reached') else 'N'} "
        f"trail_moves={pos.get('trail_moves',0)} | "
        f"reason={exit_reason} | result={result_str.upper()} | "
        f"balance_after={balance_after:.2f}"
    )


# ── MT5 actions ───────────────────────────────────────────────────────────────

def partial_close_mt5(ticket: int, volume: float, direction: str) -> bool:
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        LOG.error(f"PARTIAL CLOSE: ticket {ticket} not found in MT5")
        return False
    close_type = mt5.ORDER_TYPE_SELL if direction == "long" else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(SYMBOL)
    price = tick.bid if direction == "long" else tick.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       round(volume, 2),
        "type":         close_type,
        "position":     ticket,
        "price":        price,
        "deviation":    30,
        "magic":        MAGIC,
        "comment":      "H-TP1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(req)
    ok = result.retcode == mt5.TRADE_RETCODE_DONE
    LOG.info(f"PARTIAL CLOSE ticket={ticket} vol={volume:.2f} price={price:.2f} "
             f"retcode={result.retcode} {'OK' if ok else 'FAIL'}")
    return ok


def place_limit_order(direction: str, entry_price: float, lot: float,
                      sl: float, conn: MT5Connector) -> int:
    order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "long" else mt5.ORDER_TYPE_SELL_LIMIT
    far_tp = round(entry_price + 500 * PIP_SIZE if direction == "long"
                   else entry_price - 500 * PIP_SIZE, 2)
    req = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       SYMBOL,
        "volume":       round(lot, 2),
        "type":         order_type,
        "price":        round(entry_price, 2),
        "sl":           round(sl, 2),
        "tp":           far_tp,
        "deviation":    20,
        "magic":        MAGIC,
        "comment":      "H-A-limit",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }
    result = mt5.order_send(req)
    ok = result.retcode == mt5.TRADE_RETCODE_DONE
    ticket = result.order if ok else 0
    LOG.info(f"LIMIT ORDER {direction.upper()} @ {entry_price:.2f} lot={lot:.2f} "
             f"sl={sl:.2f} retcode={result.retcode} ticket={ticket}")
    return ticket


def cancel_order(order_ticket: int) -> bool:
    req = {"action": mt5.TRADE_ACTION_REMOVE, "order": order_ticket}
    result = mt5.order_send(req)
    ok = result.retcode == mt5.TRADE_RETCODE_DONE
    LOG.info(f"CANCEL ORDER ticket={order_ticket} {'OK' if ok else 'FAIL'}")
    return ok


def check_limit_filled(order_ticket: int) -> int:
    orders = mt5.orders_get(ticket=order_ticket)
    if orders:
        return 0
    positions = mt5.positions_get(symbol=SYMBOL) or []
    for p in positions:
        if p.magic == MAGIC:
            return p.ticket
    return 0


OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}


def build_htf_frames(bars: pd.DataFrame):
    d1 = bars.resample("1D").agg(OHLCV_AGG).dropna()
    h4 = bars.resample("4h").agg(OHLCV_AGG).dropna()
    return d1, h4


def _kill_zone_label(hour: int) -> str:
    if 7 <= hour < 10:
        return "london_kz"
    if 13 <= hour < 16:
        return "ny_kz"
    return ""


def _guard_reason(consec_losses, daily_pnl, balance, halt_cooldown_bar, bar_index) -> str:
    if consec_losses >= CONSEC_HALT and bar_index < halt_cooldown_bar:
        return f"HALT: {consec_losses} consecutive losses (cooldown)"
    if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
        return f"DAILY LOSS: {daily_pnl:.2f}"
    return ""


def _write_quick_status(now_utc, balance, equity, d1_regime, h4_dir, daily_bias,
                        asian_h, asian_l, consec_losses, daily_pnl, position):
    write_status({
        "balance":       round(balance, 2),
        "equity":        round(equity, 2),
        "updated":       now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "utc_hour":      now_utc.hour,
        "d1_regime":     d1_regime,
        "h4_direction":  h4_dir,
        "daily_bias":    daily_bias,
        "asian_h":       round(asian_h, 2) if asian_h else None,
        "asian_l":       round(asian_l, 2) if asian_l else None,
        "in_session":    True,
        "in_kill_zone":  _kill_zone_label(now_utc.hour),
        "has_position":  position is not None,
        "consec_losses": consec_losses,
        "daily_pnl":     round(daily_pnl, 2),
    })


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    LOG.info("=" * 60)
    LOG.info("STRATEGY H — INSTITUTIONAL SESSION FLOW  |  XAUUSD M15")
    LOG.info("=" * 60)

    conn = MT5Connector(
        login=config.LOGIN,
        password=config.PASSWORD,
        server=config.SERVER,
        symbol=SYMBOL,
    )
    acct = conn.connect()

    if not conn.is_demo():
        LOG.error("NOT A DEMO ACCOUNT — refusing to start. Strategy H is demo-only.")
        conn.shutdown()
        return

    balance = float(acct.balance)
    LOG.info(f"Account: {acct.login}  Balance: ${balance:.2f}  Server: {config.SERVER}")
    LOG.info(f"Log dir: {LOG_DIR}")
    LOG.info(f"Trades : {TRADES_CSV}")
    LOG.info("")

    # ── State ─────────────────────────────────────────────────────────────────
    persisted     = load_state()
    position      = persisted.get("position", None)
    pending       = None
    bar_index     = 0
    prev_bar_time = None
    consec_losses = 0
    halt_cooldown_bar = 0
    daily_pnl     = 0.0
    daily_date    = None

    d1_regime  = D1_RANGE
    h4_dir     = H4_NONE
    daily_bias = BIAS_NEUTRAL
    last_ctx_date = None

    ab_h = -1e18; ab_l = 1e18
    asian_h = None; asian_l = None
    pdh = None; pdl = None; pdc = None
    d1rh = None; d1rl = None
    d1_all = None; h4_all = None

    if position:
        LOG.info(f"RESTORED position from disk: {position['direction'].upper()} "
                 f"Pattern-{position.get('pattern','?')} @ {position.get('entry_price','?')} "
                 f"SL={position.get('current_sl','?')}")

    LOG.info("Live loop started. Ctrl+C to stop.")
    _startup_done = False   # flag: context initialised from history

    while True:
        try:
            loop_start = time.time()
            now_utc    = datetime.now(timezone.utc)
            hour       = now_utc.hour
            cur_date   = now_utc.date()

            # ── Fetch M15 bars ────────────────────────────────────────────────
            try:
                bars = conn.get_candles("M15", count=700, symbol=SYMBOL)
                if bars.index.tzinfo is None:
                    bars.index = bars.index.tz_localize("UTC")
            except Exception as e:
                LOG.error(f"FETCH ERROR: {e}")
                time.sleep(LOOP_SLEEP)
                continue

            if len(bars) < 50:
                time.sleep(LOOP_SLEEP)
                continue

            # ── Detect new M15 bar ────────────────────────────────────────────
            last_bar_time = bars.index[-1]
            if last_bar_time == prev_bar_time:
                acct_info = mt5.account_info()
                balance   = float(acct_info.balance)
                equity    = float(acct_info.equity)
                _write_quick_status(now_utc, balance, equity, d1_regime, h4_dir,
                                    daily_bias, asian_h, asian_l, consec_losses,
                                    daily_pnl, position)
                elapsed = time.time() - loop_start
                time.sleep(max(1.0, LOOP_SLEEP - elapsed))
                continue

            prev_bar_time = last_bar_time
            bar_index    += 1

            bar   = bars.iloc[-2]   # completed bar
            bar_h = float(bar["high"])
            bar_l = float(bar["low"])
            bar_c = float(bar["close"])
            bar_t = bars.index[-2]
            bar_hour = bar_t.hour
            bar_date = bar_t.date()

            atr_window = bars.iloc[:-1]
            atr        = _atr(atr_window, ATR_PERIOD)
            atr_avg    = _atr(atr_window, min(ATR_AVG_PERIOD, len(atr_window)))

            # Log every new bar at DEBUG level
            LOG.debug(
                f"BAR[{bar_index}] {str(bar_t)[:16]} | "
                f"O={bar['open']:.2f} H={bar_h:.2f} L={bar_l:.2f} C={bar_c:.2f} | "
                f"ATR={atr:.2f} ATRavg={atr_avg:.2f} | "
                f"D1={d1_regime} H4={h4_dir} bias={daily_bias}"
            )

            # ── Daily reset ───────────────────────────────────────────────────
            if bar_date != daily_date:
                daily_date = bar_date
                daily_pnl  = 0.0
                LOG.info(f"--- New trading day: {bar_date}  consec_losses={consec_losses} ---")

            # ── One-time startup context init (first bar, any hour) ──────────
            if not _startup_done:
                _startup_done = True
                _init_bars = bars.iloc[:-1]   # all completed bars

                # D1 / H4 regime
                _d1a, _h4a = build_htf_frames(_init_bars)
                _d1b = _d1a[_d1a.index.date < bar_date]
                if len(_d1b) >= 12:
                    d1_regime = compute_d1_regime(_d1b)
                if len(_d1b) >= 2:
                    pdh = float(_d1b.iloc[-1]["high"])
                    pdl = float(_d1b.iloc[-1]["low"])
                    pdc = float(_d1b.iloc[-1]["close"])
                    _rec = _d1b.iloc[-min(20, len(_d1b)):]
                    d1rh = float(_rec["high"].max())
                    d1rl = float(_rec["low"].min())
                    LOG.info(f"STARTUP | D1 bars={len(_d1b)} regime={d1_regime} "
                             f"d1rh={d1rh:.2f} d1rl={d1rl:.2f}")

                _h4cut = pd.Timestamp(year=bar_date.year, month=bar_date.month,
                                      day=bar_date.day, hour=bar_hour, tzinfo=timezone.utc)
                _h4b = _h4a[_h4a.index + pd.Timedelta(hours=4) <= _h4cut]
                if len(_h4b) >= 6:
                    h4_dir = compute_h4_direction(_h4b)

                daily_bias, _ = compute_daily_bias(
                    d1_regime, h4_dir, bar_c,
                    d1rh, d1rl, pdh, pdl, pdc,
                )

                # Asian range — recover from last completed Asian session in history
                # Asian session = 22:00 previous day through 07:00 today
                _ar_mask = (_init_bars.index.hour >= 22) | (_init_bars.index.hour < 7)
                _ar_bars  = _init_bars[_ar_mask]
                if len(_ar_bars) >= 4:
                    asian_h = float(_ar_bars["high"].max())
                    asian_l = float(_ar_bars["low"].min())
                    LOG.info(f"STARTUP | Asian range recovered from history: "
                             f"{asian_l:.2f} / {asian_h:.2f} ({len(_ar_bars)} bars)")

                last_ctx_date = bar_date   # prevent double-init at 07:00 same day
                LOG.info(f"STARTUP CONTEXT | D1={d1_regime} H4={h4_dir} bias={daily_bias} | "
                         f"AsianRange={'ready' if asian_h else 'not yet'}")

            # ── Asian range accumulation ──────────────────────────────────────
            if bar_hour >= 22 or bar_hour < 7:
                ab_h = max(ab_h, bar_h)
                ab_l = min(ab_l, bar_l)

            # ── Daily context at 07:00 UTC ────────────────────────────────────
            if bar_hour == 7 and bar_date != last_ctx_date:
                last_ctx_date = bar_date

                if ab_h > ab_l and ab_h != -1e18:
                    asian_h = ab_h
                    asian_l = ab_l
                ab_h = -1e18; ab_l = 1e18

                d1_all, h4_all = build_htf_frames(bars.iloc[:-1])

                d1_before = d1_all[d1_all.index.date < bar_date]
                if len(d1_before) >= 2:
                    pdh  = float(d1_before.iloc[-1]["high"])
                    pdl  = float(d1_before.iloc[-1]["low"])
                    pdc  = float(d1_before.iloc[-1]["close"])
                    rec  = d1_before.iloc[-min(20, len(d1_before)):]
                    d1rh = float(rec["high"].max())
                    d1rl = float(rec["low"].min())

                if len(d1_before) >= 12:
                    d1_regime = compute_d1_regime(d1_before)

                h4_cutoff = pd.Timestamp(year=bar_date.year, month=bar_date.month,
                                         day=bar_date.day, hour=7, tzinfo=timezone.utc)
                h4_before = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4_cutoff]
                if len(h4_before) >= 12:
                    h4_dir = compute_h4_direction(h4_before)

                if d1rh is not None and d1rl is not None:
                    daily_bias, _ = compute_daily_bias(
                        d1_regime, h4_dir, bar_c,
                        d1rh, d1rl, pdh, pdl, pdc,
                    )

                LOG.info(
                    f"DAILY CONTEXT | {bar_date} | D1={d1_regime} H4={h4_dir} "
                    f"bias={daily_bias} | "
                    f"AsianRange={asian_h:.1f}/{asian_l:.1f}" if asian_h else
                    f"DAILY CONTEXT | {bar_date} | D1={d1_regime} H4={h4_dir} "
                    f"bias={daily_bias} | AsianRange=building"
                )

            acct_info = mt5.account_info()
            balance   = float(acct_info.balance)
            equity    = float(acct_info.equity)

            # ── Check pending limit order (Pattern A) ─────────────────────────
            if pending is not None and position is None:
                bars_since = bar_index - pending["created_bar"]
                if bars_since > PATTERN_A_EXPIRE:
                    if pending.get("order_ticket"):
                        cancel_order(pending["order_ticket"])
                    LOG.info(f"PENDING A expired after {bars_since} bars")
                    pending = None
                else:
                    if pending.get("order_ticket"):
                        filled_ticket = check_limit_filled(pending["order_ticket"])
                        if filled_ticket:
                            tick = mt5.symbol_info_tick(SYMBOL)
                            fill_price = tick.ask if pending["direction"] == "long" else tick.bid
                            sl  = compute_sl(fill_price, pending["direction"],
                                             pending["atr"], pending.get("sweep_extreme"))
                            tp1 = compute_tp1(fill_price, sl, pending["direction"])
                            position = {
                                "ticket":       filled_ticket,
                                "direction":    pending["direction"],
                                "entry_price":  fill_price,
                                "sl":           sl,
                                "tp1":          tp1,
                                "current_sl":   sl,
                                "lot":          pending["lot"],
                                "half_lot":     max(0.01, round(pending["lot"] * 0.5, 2)),
                                "open_bar":     bar_index,
                                "tp1_reached":  False,
                                "tp1_pnl":      0.0,
                                "pattern":      "A",
                                "atr_at_entry": pending["atr"],
                                "entry_time":   bar_t.isoformat(),
                                "d1_regime":    d1_regime,
                                "h4_dir":       h4_dir,
                                "daily_bias":   daily_bias,
                                "session":      pending.get("session", "?"),
                                "balance_before": balance,
                                "sl_distance":  abs(fill_price - sl),
                                "best_price":   fill_price,
                                "trail_moves":  0,
                            }
                            save_state({"position": position})
                            pending = None
                            LOG.info(
                                f"ENTRY | Pattern A FILLED | {position['direction'].upper()} "
                                f"@ {fill_price:.2f} | SL={sl:.2f} TP1={tp1:.2f} "
                                f"lot={position['lot']:.2f} | "
                                f"D1={d1_regime} H4={h4_dir} bias={daily_bias}"
                            )

            # ── Manage open position ──────────────────────────────────────────
            if position is not None:
                pos       = position
                direction = pos["direction"]
                bars_held = bar_index - pos["open_bar"]

                tick    = mt5.symbol_info_tick(SYMBOL)
                cur_bid = tick.bid
                cur_ask = tick.ask
                cur_price = cur_bid if direction == "long" else cur_ask

                # Update best price seen (for trailing)
                old_best = pos.get("best_price", pos["entry_price"])
                if direction == "long":
                    pos["best_price"] = max(old_best, bar_h)   # use bar high
                else:
                    pos["best_price"] = min(old_best, bar_l)   # use bar low

                sl_hit  = check_sl_hit(bar_l, bar_h, pos["current_sl"], direction)
                tp1_hit = (not pos["tp1_reached"] and
                           check_tp1_hit(bar_l, bar_h, pos["tp1"], direction))

                exit_reason = None
                exit_price  = None
                pnl_total   = 0.0

                if sl_hit:
                    fill = pos["current_sl"] - SLIPPAGE if direction == "long" \
                           else pos["current_sl"] + SLIPPAGE
                    remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]

                    ok, _ = conn.close_position(pos["ticket"])
                    if ok:
                        pnl_half  = _pnl_usd(direction, pos["entry_price"], fill, remaining)
                        pnl_total = pnl_half + pos.get("tp1_pnl", 0.0)
                        exit_reason = "SL_HIT"
                        exit_price  = round(fill, 2)
                        result_str  = "win" if pnl_total > 0 else "loss"

                        balance   += pnl_total
                        daily_pnl += pnl_total
                        if result_str == "win":
                            consec_losses = 0
                            halt_cooldown_bar = 0
                        else:
                            consec_losses += 1

                        _log_trade(pos, exit_price, exit_reason, pnl_total, bars_held, result_str, balance)
                        position = None
                        save_state({})

                elif tp1_hit:
                    tp1_fill = pos["tp1"]
                    tp1_pnl  = _pnl_usd(direction, pos["entry_price"], tp1_fill, pos["half_lot"])
                    ok = partial_close_mt5(pos["ticket"], pos["half_lot"], direction)
                    if ok:
                        pos["tp1_reached"]  = True
                        pos["tp1_pnl"]      = tp1_pnl
                        pos["current_sl"]   = pos["entry_price"]   # breakeven
                        # best_price already updated above (bar_h / bar_l)
                        position = pos
                        save_state({"position": position})
                        conn.modify_sl(pos["ticket"], round(pos["entry_price"], 2))
                        LOG.info(
                            f"TP1 HIT | {direction.upper()} +{tp1_pnl:.2f} | "
                            f"SL→BREAKEVEN={pos['entry_price']:.2f} | "
                            f"remaining={pos['half_lot']:.2f} lots | "
                            f"best_price={pos['best_price']:.2f}"
                        )

                elif pos["tp1_reached"]:
                    # ── Trailing SL (after TP1) ───────────────────────────────
                    m15_slice = bars.iloc[max(0, len(bars) - 32):-1]
                    new_sl = update_trail_sl(pos, m15_slice, atr, current_price=pos["best_price"])

                    if new_sl != pos["current_sl"]:
                        old_sl = pos["current_sl"]
                        pos["current_sl"] = new_sl
                        pos["trail_moves"] = pos.get("trail_moves", 0) + 1
                        position = pos
                        save_state({"position": position})
                        conn.modify_sl(pos["ticket"], round(new_sl, 2))
                        LOG.info(
                            f"TRAIL SL | {direction.upper()} | "
                            f"SL: {old_sl:.2f} → {new_sl:.2f} | "
                            f"best={pos['best_price']:.2f} ATR={atr:.2f} | "
                            f"move #{pos['trail_moves']}"
                        )

                    # Regime exit (after TP1 only)
                    if check_regime_exit(pos, d1_regime, h4_dir):
                        fill = cur_bid if direction == "long" else cur_ask
                        ok, _ = conn.close_position(pos["ticket"])
                        if ok:
                            pnl_half  = _pnl_usd(direction, pos["entry_price"], fill, pos["half_lot"])
                            pnl_total = pnl_half + pos["tp1_pnl"]
                            exit_reason = "REGIME_EXIT"
                            exit_price  = round(fill, 2)
                            result_str  = "win" if pnl_total > 0 else "loss"
                            balance   += pnl_total
                            daily_pnl += pnl_total
                            consec_losses = 0 if result_str == "win" else consec_losses + 1
                            _log_trade(pos, exit_price, exit_reason, pnl_total, bars_held, result_str, balance)
                            position = None
                            save_state({})

                elif check_time_stop(bars_held, pos["tp1_reached"]):
                    fill = cur_bid if direction == "long" else cur_ask
                    ok, _ = conn.close_position(pos["ticket"])
                    if ok:
                        pnl_total   = _pnl_usd(direction, pos["entry_price"], fill, pos["lot"])
                        exit_reason = "TIME_STOP"
                        exit_price  = round(fill, 2)
                        result_str  = "win" if pnl_total > 0 else "loss"
                        balance   += pnl_total
                        daily_pnl += pnl_total
                        if result_str == "win":
                            consec_losses = 0
                        else:
                            consec_losses += 1
                        _log_trade(pos, exit_price, exit_reason, pnl_total, bars_held, result_str, balance)
                        position = None
                        save_state({})
                        LOG.info(f"TIME STOP | bars={bars_held} PnL={pnl_total:+.2f}")

                if position is not None:
                    save_state({"position": position})

            # ── Pattern detection ─────────────────────────────────────────────
            if position is None and pending is None:
                # ── Guard checks (log reasons at DEBUG) ───────────────────────
                if daily_pnl < -(balance * DAILY_LOSS_LIMIT):
                    LOG.debug(f"SKIP | daily loss limit hit: {daily_pnl:.2f}")

                elif consec_losses >= CONSEC_HALT:
                    if halt_cooldown_bar == 0:
                        halt_cooldown_bar = bar_index + HALT_COOLDOWN
                        LOG.info(f"HALT | {consec_losses} consecutive losses | "
                                 f"cooldown until bar {halt_cooldown_bar} ({HALT_COOLDOWN} bars = 24h)")
                    if bar_index >= halt_cooldown_bar:
                        LOG.info(f"HALT LIFTED | resetting consec_losses {consec_losses} → {CONSEC_REDUCE}")
                        consec_losses     = CONSEC_REDUCE
                        halt_cooldown_bar = 0
                    else:
                        LOG.debug(f"SKIP | HALT cooldown active (bar {bar_index} < {halt_cooldown_bar})")

                elif d1_regime in (D1_CHOP, D1_COMPRESS):
                    LOG.debug(f"SKIP | D1 regime={d1_regime} (no-trade zone)")

                elif atr_avg > 0 and (atr > 3.0 * atr_avg or atr < 0.4 * atr_avg):
                    LOG.debug(f"SKIP | ATR extreme: atr={atr:.2f} avg={atr_avg:.2f}")

                else:
                    tick   = mt5.symbol_info_tick(SYMBOL)
                    spread = (tick.ask - tick.bid) / PIP_SIZE
                    if spread > SPREAD_MAX:
                        LOG.debug(f"SKIP | spread={spread:.1f} > max={SPREAD_MAX}")
                    else:
                        risk_pct   = RISK_PCT_REDUCED if consec_losses >= CONSEC_REDUCE else RISK_PCT
                        m15_slice  = bars.iloc[:-1]
                        setups     = []

                        for direction in ("long", "short"):
                            if not bias_allows_direction(daily_bias, direction):
                                LOG.debug(f"BIAS BLOCK | {direction} blocked by bias={daily_bias}")
                                continue

                            sc = detect_pattern_c(m15_slice, direction, atr)
                            if sc:
                                setups.append(sc)
                                LOG.debug(f"PATTERN C {direction.upper()} found: entry={sc.get('entry_price',0):.2f}")

                            sa = detect_pattern_a(
                                m15_slice, bar_hour, direction,
                                asian_h, asian_l, pdh, pdl, atr,
                            )
                            if sa:
                                setups.append(sa)
                                LOG.debug(f"PATTERN A {direction.upper()} found: entry={sa.get('entry_price',0):.2f}")

                            sb = detect_pattern_b(m15_slice, direction, atr)
                            if sb:
                                setups.append(sb)
                                LOG.debug(f"PATTERN B {direction.upper()} found: entry={sb.get('entry_price',0):.2f}")

                        if not setups:
                            _scanned = [d for d in ("long","short")
                                        if bias_allows_direction(daily_bias, d)]
                            _diag = " || ".join(
                                f"{d}: {scan_diagnostic(m15_slice, d, atr)}"
                                for d in _scanned
                            )
                            LOG.debug(f"SCAN | no patterns on bar {bar_index} | "
                                      f"session={_kill_zone_label(bar_hour) or 'in-session'} "
                                      f"spread={spread:.1f} | {_diag}")

                        best = pattern_priority(setups)
                        if best is not None:
                            direction   = best["direction"]
                            entry_price = float(best["entry_price"])
                            sl  = compute_sl(entry_price, direction, atr,
                                             best.get("sweep_extreme"))
                            tp1 = compute_tp1(entry_price, sl, direction)

                            if not sl_distance_valid(entry_price, sl, atr):
                                LOG.debug(f"SKIP | SL distance invalid: {abs(entry_price-sl):.2f} "
                                          f"(ATR={atr:.2f} min={1.5*atr:.2f} max={3.0*atr:.2f})")
                                best = None

                        if best is not None:
                            lot   = compute_lot(balance, abs(entry_price - sl), risk_pct)
                            ptype = best["type"]

                            LOG.info(
                                f"SETUP | Pattern {ptype} {direction.upper()} | "
                                f"entry={entry_price:.2f} SL={sl:.2f} TP1={tp1:.2f} | "
                                f"lot={lot:.2f} risk={risk_pct*100:.1f}% | "
                                f"D1={d1_regime} H4={h4_dir} bias={daily_bias} | "
                                f"ATR={atr:.2f} spread={spread:.1f} session={_kill_zone_label(bar_hour) or 'in-session'}"
                            )

                            if ptype == "A":
                                order_ticket = place_limit_order(direction, entry_price, lot, sl, conn)
                                if order_ticket:
                                    pending = {
                                        "type":          "A",
                                        "direction":     direction,
                                        "entry_price":   entry_price,
                                        "lot":           lot,
                                        "atr":           atr,
                                        "sl":            sl,
                                        "tp1":           tp1,
                                        "sweep_extreme": best.get("sweep_extreme"),
                                        "session":       best.get("session", "?"),
                                        "created_bar":   bar_index,
                                        "order_ticket":  order_ticket,
                                    }
                            else:
                                far_tp = round(entry_price + 500 * PIP_SIZE if direction == "long"
                                               else entry_price - 500 * PIP_SIZE, 2)
                                ok, result = conn.place_order(
                                    "buy" if direction == "long" else "sell",
                                    lot, sl, far_tp, symbol=SYMBOL
                                )
                                if ok:
                                    time.sleep(0.5)
                                    positions = mt5.positions_get(symbol=SYMBOL) or []
                                    for p in positions:
                                        if p.magic == MAGIC:
                                            actual_fill = p.price_open
                                            sl2  = compute_sl(actual_fill, direction, atr,
                                                               best.get("sweep_extreme"))
                                            tp1_2 = compute_tp1(actual_fill, sl2, direction)
                                            conn.modify_sl(p.ticket, round(sl2, 2))
                                            position = {
                                                "ticket":        p.ticket,
                                                "direction":     direction,
                                                "entry_price":   actual_fill,
                                                "sl":            sl2,
                                                "tp1":           tp1_2,
                                                "current_sl":    sl2,
                                                "lot":           lot,
                                                "half_lot":      max(0.01, round(lot * 0.5, 2)),
                                                "open_bar":      bar_index,
                                                "tp1_reached":   False,
                                                "tp1_pnl":       0.0,
                                                "pattern":       ptype,
                                                "atr_at_entry":  atr,
                                                "entry_time":    bar_t.isoformat(),
                                                "d1_regime":     d1_regime,
                                                "h4_dir":        h4_dir,
                                                "daily_bias":    daily_bias,
                                                "session":       best.get("session", "?"),
                                                "balance_before": balance,
                                                "sl_distance":   abs(actual_fill - sl2),
                                                "best_price":    actual_fill,
                                                "trail_moves":   0,
                                            }
                                            save_state({"position": position})
                                            LOG.info(
                                                f"ENTRY | Pattern {ptype} {direction.upper()} "
                                                f"@ {actual_fill:.2f} | "
                                                f"SL={sl2:.2f} TP1={tp1_2:.2f} lot={lot:.2f} | "
                                                f"ticket={p.ticket} | "
                                                f"D1={d1_regime} H4={h4_dir} bias={daily_bias} | "
                                                f"ATR={atr:.2f} bar={bar_index}"
                                            )
                                            break
                                else:
                                    LOG.error(f"ORDER FAILED | Pattern {ptype} {direction.upper()} "
                                              f"retcode={getattr(result,'retcode','?')}")

            # ── Write status ──────────────────────────────────────────────────
            pos_info = None
            if position is not None:
                tick = mt5.symbol_info_tick(SYMBOL)
                cur  = tick.bid if position["direction"] == "long" else tick.ask
                pos_info = {
                    "ticket":      position["ticket"],
                    "direction":   position["direction"],
                    "entry_price": position["entry_price"],
                    "sl":          position["current_sl"],
                    "tp1":         position["tp1"],
                    "lot":         position["lot"],
                    "pattern":     position["pattern"],
                    "bars_held":   bar_index - position["open_bar"],
                    "tp1_reached": position["tp1_reached"],
                    "best_price":  position.get("best_price", position["entry_price"]),
                    "trail_moves": position.get("trail_moves", 0),
                    "open_pnl":    _pnl_usd(position["direction"], position["entry_price"], cur,
                                             position["half_lot"] if position["tp1_reached"]
                                             else position["lot"]) + position.get("tp1_pnl", 0.0),
                }

            write_status({
                "balance":       round(balance, 2),
                "equity":        round(equity, 2),
                "updated":       now_utc.strftime("%Y-%m-%d %H:%M UTC"),
                "utc_hour":      now_utc.hour,
                "d1_regime":     d1_regime,
                "h4_direction":  h4_dir,
                "daily_bias":    daily_bias,
                "atr":           round(atr, 3),
                "asian_h":       round(asian_h, 2) if asian_h else None,
                "asian_l":       round(asian_l, 2) if asian_l else None,
                "in_session":    True,
                "in_kill_zone":  _kill_zone_label(hour),
                "has_position":  position is not None,
                "position":      pos_info,
                "pending_pattern": pending.get("type") if pending else None,
                "consec_losses": consec_losses,
                "daily_pnl":     round(daily_pnl, 2),
                "guard_block":   _guard_reason(consec_losses, daily_pnl, balance,
                                               halt_cooldown_bar, bar_index),
            })

            elapsed = time.time() - loop_start
            time.sleep(max(1.0, LOOP_SLEEP - elapsed))

        except KeyboardInterrupt:
            LOG.info("Ctrl+C — shutting down cleanly.")
            break
        except Exception as e:
            import traceback
            LOG.error(f"LOOP ERROR: {e}")
            LOG.debug(traceback.format_exc())
            time.sleep(LOOP_SLEEP)

    conn.shutdown()
    LOG.info("Bot stopped.")


if __name__ == "__main__":
    main()
