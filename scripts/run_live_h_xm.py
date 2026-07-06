#!/usr/bin/env python3
"""
scripts/run_live_h_xm.py  --  Strategy H on XM: GOLD + BTCUSD
=============================================================
Trades TWO instruments on XM simultaneously:
  - GOLD  : weekdays 06-19 UTC (London/NY kill zones)
  - BTCUSD: 24/7 every day incl. weekends (Asia/London/NY kill zones)

Usage (DEMO only):
    py -3 scripts/run_live_h_xm.py

Data files:
  data/h_xm_GOLD_trades.csv    -- GOLD trade log
  data/h_xm_BTC_trades.csv     -- BTC trade log
  data/h_xm_live_status.json   -- combined dashboard status
  data/h_xm_GOLD_state.json    -- GOLD open position
  data/h_xm_BTC_state.json     -- BTC open position
  data/logs/h_xm_YYYY-MM-DD.log
"""

import sys, os, time, csv, json, logging
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config_xm as cfg
import MetaTrader5 as mt5

from src.mt5_connector import MT5Connector
from src.strategy_h.regime import (
    compute_d1_regime, compute_h4_direction, compute_daily_bias,
    bias_allows_direction,
    D1_BULL, D1_BEAR, D1_RANGE, D1_CHOP, D1_COMPRESS,
    H4_NONE, BIAS_NEUTRAL,
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

# ── XM terminal ───────────────────────────────────────────────────────────────
XM_TERMINAL = r"C:\Program Files\XM Global MT5\terminal64.exe"

# ── Instrument definitions ────────────────────────────────────────────────────
INSTRUMENTS = {
    "GOLD": {
        "symbol":       "GOLD",
        "pip_size":     0.1,
        "pip_val":      10.0,
        "spread_max":   6.5,   # R1: raised 3.0→6.5 — XM live spread is 5.4–5.9 pips (6.5 allows normal, blocks news)
        "session_start": 0,
        "session_end":  24,
        "weekdays_only": True,
        "magic":        202411,
        "slippage":     0.05,
        "label":        "GOLD",
        "trades_csv":   "h_xm_GOLD_trades.csv",
        "state_json":   "h_xm_GOLD_state.json",
        # Tuned params (from backtest optimisation + pre-live review)
        "tp1_rr":            1.2,    # R2: lowered 1.5→1.2 — time-stop WR 65.9% shows 1.5 overshoots
        "pattern_b_only":    True,   # skip Pattern C (PF barely positive for GOLD)
        "regime_dir_filter": True,   # BULL→longs only, BEAR→shorts only, RANGE→both
        "halt_reset_zero":   True,   # reset consec to 0 after halt (clean slate)
        "daily_loss_limit":  0.015,  # 1.5% per day (tighter than BTC's 2%)
        "atr_low_factor":    0.5,    # skip if atr < 0.5×avg (filters dead Asian hours)
        "risk_pct":          0.0075, # R3: reduced 1.0%→0.75% — MaxDD 61.7% unacceptable for live
        "risk_pct_reduced":  0.004,  # reduced-risk level when consec losses ≥ 3
        "consec_halt":       5,      # R7: lowered 6→5 — earlier circuit breaker for GOLD's DD risk
        "be_buffer_atr":     0.0,    # no BE buffer for GOLD (smaller intra-bar volatility)
    },
    "BTC": {
        "symbol":       "BTCUSD",
        "pip_size":     1.0,
        "pip_val":      1.0,
        "spread_max":   65.0,  # R6: raised 50→65 — live spread is consistently at 50, needs buffer
        "session_start": 0,    # 24/7 all week
        "session_end":  24,
        "weekdays_only": False,
        "magic":        202412,
        "slippage":     0.50,
        "label":        "BTC",
        "trades_csv":   "h_xm_BTC_trades.csv",
        "state_json":   "h_xm_BTC_state.json",
        "be_buffer_atr": 0.25,  # R4: BE at entry−0.25×ATR after TP1 — absorbs brief pullbacks
    },
}

# ── Risk params ───────────────────────────────────────────────────────────────
RISK_PCT         = 0.01
RISK_PCT_REDUCED = 0.005
DAILY_LOSS_LIMIT = 0.02
CONSEC_REDUCE    = 3
CONSEC_HALT      = 6
HALT_COOLDOWN    = 96
PATTERN_A_EXPIRE = 4
ATR_PERIOD       = 14
ATR_AVG_PERIOD   = 20
LOOP_SLEEP       = 30

BASE     = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR  = os.path.join(DATA_DIR, "logs")
STATUS_JSON = os.path.join(DATA_DIR, "h_xm_live_status.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

TRADE_FIELDS = [
    "open_time", "close_time", "direction", "pattern", "session",
    "entry_price", "sl", "tp1", "exit_price", "lot", "sl_distance",
    "pnl", "pnl_r", "result", "exit_reason", "d1_regime", "h4_dir",
    "daily_bias", "atr_at_entry", "bars_held", "tp1_reached", "tp1_pnl",
    "balance_before", "balance_after", "best_price_seen", "trail_moves", "symbol",
]


# ── Logging ───────────────────────────────────────────────────────────────────
def _make_logger():
    logger = logging.getLogger("h_xm")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    log_path = os.path.join(LOG_DIR, f"h_xm_{today}.log")
    # Rotate at UTC midnight — new file named h_xm_YYYY-MM-DD.log each day
    fh = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1, backupCount=30,
        encoding="utf-8", utc=True,
    )
    fh.suffix = "%Y-%m-%d"
    fh.namer  = lambda name: os.path.join(
        LOG_DIR, "h_xm_{}.log".format(os.path.basename(name).rsplit(".", 1)[-1])
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s UTC | %(levelname)-5s | %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("  %(message)s"))
    logger.addHandler(fh); logger.addHandler(ch)
    return logger

LOG = _make_logger()

# ── Helpers ───────────────────────────────────────────────────────────────────
OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last"}

def _atr(df, p=14):
    import numpy as np
    if len(df) < 2: return 1.0
    h = df["high"].values[-p-2:]; l = df["low"].values[-p-2:]; c = df["close"].values[-p-2:]
    if len(h) < 2: return max(float(h[-1]-l[-1]), 1e-9)
    tr = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
    use = tr[-min(p, len(tr)):]
    return float(use.mean()) if len(use) > 0 else 1.0

def _tp1_price(entry, sl, direction, rr=1.0):
    dist = abs(entry - sl)
    return round(entry + rr * dist if direction == "long" else entry - rr * dist, 5)

def _pnl_usd(direction, entry, exit_p, lot, pip_size, pip_val):
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / pip_size * pip_val * lot, 2)

def _pnl_r(direction, entry, sl, exit_p):
    d = abs(entry - sl)
    if d == 0: return 0.0
    pts = (exit_p - entry) if direction == "long" else (entry - exit_p)
    return round(pts / d, 3)

def build_htf(bars):
    return bars.resample("1D").agg(OHLCV_AGG).dropna(), bars.resample("4h").agg(OHLCV_AGG).dropna()

def write_status(data):
    try:
        tmp = STATUS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, STATUS_JSON)
    except Exception as e:
        LOG.error(f"STATUS write: {e}")

def load_state(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_state(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

def append_trade(csv_path, row):
    new_file = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        if new_file: w.writeheader()
        w.writerow(row)

def _log_trade(instr, pos, exit_price, exit_reason, pnl_total, bars_held, result_str, balance_after):
    pip_size = INSTRUMENTS[instr]["pip_size"]
    pip_val  = INSTRUMENTS[instr]["pip_val"]
    symbol   = INSTRUMENTS[instr]["symbol"]
    sl_dist  = pos.get("sl_distance", abs(pos["entry_price"] - pos["sl"]))
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
        "pnl_r":           _pnl_r(pos["direction"], pos["entry_price"], pos["sl"], exit_price),
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
        "symbol":          symbol,
    }
    append_trade(os.path.join(DATA_DIR, INSTRUMENTS[instr]["trades_csv"]), row)
    LOG.info(f"TRADE CLOSED [{instr}] | {pos['direction'].upper()} Pattern-{pos.get('pattern','?')} | "
             f"entry={pos['entry_price']:.2f} exit={exit_price:.2f} | "
             f"PnL={pnl_total:+.2f} ({row['pnl_r']:+.2f}R) | "
             f"bars={bars_held} reason={exit_reason} {result_str.upper()} | bal=${balance_after:.2f}")


# ── Per-instrument slot ───────────────────────────────────────────────────────
class InstrumentSlot:
    """All mutable state for one traded instrument."""
    def __init__(self, instr_key, slot_idx=0):
        self.key          = instr_key
        self.slot_idx     = slot_idx          # 0=primary, 1=secondary (scale-in)
        cfg_i             = INSTRUMENTS[instr_key]
        self.symbol       = cfg_i["symbol"]
        self.pip_size     = cfg_i["pip_size"]
        self.pip_val      = cfg_i["pip_val"]
        self.spread_max   = cfg_i["spread_max"]
        self.ses_start    = cfg_i["session_start"]
        self.ses_end      = cfg_i["session_end"]
        self.weekdays_only= cfg_i["weekdays_only"]
        # slot 0 uses base magic; slot 1 adds 2 (GOLD: 202411→202413, BTC: 202412→202414)
        self.magic        = cfg_i["magic"] + slot_idx * 2
        self.slippage     = cfg_i["slippage"]
        _state_json = cfg_i["state_json"].replace(".json", f"_{slot_idx}.json") if slot_idx > 0 else cfg_i["state_json"]
        self.state_path   = os.path.join(DATA_DIR, _state_json)

        # Restore from disk
        saved = load_state(self.state_path)
        self.position  = saved.get("position", None)
        self.pending   = None
        self.bar_index = 0
        self.prev_bar  = None

        self.consec_losses    = 0
        self.halt_cooldown_bar = 0
        self.daily_pnl        = 0.0
        self.daily_date       = None

        self.d1_regime  = D1_RANGE
        self.h4_dir     = H4_NONE
        self.daily_bias = BIAS_NEUTRAL
        self.last_ctx   = None
        self.last_entry_bar = -1              # guards against instant-close → same-bar re-entry
        self.last_entry_bar_time = saved.get("last_entry_bar_time", "")  # persists across restarts

        self.ab_h = -1e18; self.ab_l = 1e18
        self.asian_h = None; self.asian_l = None
        self.pdh = None; self.pdl = None; self.pdc = None
        self.d1rh = None; self.d1rl = None
        self._startup = False

        # Per-instrument tuning params
        self.tp1_rr             = cfg_i.get("tp1_rr", 1.0)
        self.pattern_b_only     = cfg_i.get("pattern_b_only", False)
        self.regime_dir_filter  = cfg_i.get("regime_dir_filter", False)
        self.halt_reset_zero    = cfg_i.get("halt_reset_zero", False)
        self.daily_loss_lim_pct = cfg_i.get("daily_loss_limit", DAILY_LOSS_LIMIT)
        self.atr_low_factor     = cfg_i.get("atr_low_factor", 0.4)
        self.risk_pct           = cfg_i.get("risk_pct", RISK_PCT)
        self.risk_pct_reduced   = cfg_i.get("risk_pct_reduced", RISK_PCT_REDUCED)
        self.consec_halt        = cfg_i.get("consec_halt", CONSEC_HALT)
        self.be_buffer_atr      = cfg_i.get("be_buffer_atr", 0.0)

        if self.position:
            _lbl = f"{self.key}b" if slot_idx > 0 else self.key
            LOG.info(f"RESTORED [{_lbl}] {self.position['direction'].upper()} "
                     f"Pattern-{self.position.get('pattern','?')} @ {self.position.get('entry_price','?')}")

    def in_session(self, now_utc):
        """True if current time is within this instrument's trading session."""
        wd = now_utc.weekday()
        if self.weekdays_only and wd >= 5:   # Sat=5, Sun=6
            return False
        h = now_utc.hour
        return self.ses_start <= h < self.ses_end

    def save(self):
        data = {"position": self.position} if self.position else {}
        if self.last_entry_bar_time:
            data["last_entry_bar_time"] = self.last_entry_bar_time
        save_state(self.state_path, data)

    def partial_close(self, ticket, volume, direction):
        close_type = mt5.ORDER_TYPE_SELL if direction == "long" else mt5.ORDER_TYPE_BUY
        tick  = mt5.symbol_info_tick(self.symbol)
        price = tick.bid if direction == "long" else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
            "volume": round(volume, 2), "type": close_type, "position": ticket,
            "price": price, "deviation": 30, "magic": self.magic,
            "comment": f"H-TP1-{self.key}",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(req)
        ok = r.retcode == mt5.TRADE_RETCODE_DONE
        LOG.info(f"PARTIAL CLOSE [{self.key}] ticket={ticket} vol={volume:.2f} {'OK' if ok else 'FAIL'} rc={r.retcode}")
        return ok


# ── Process one new M15 bar for one instrument ────────────────────────────────
def process_bar(slot: InstrumentSlot, bars: pd.DataFrame,
                bar_index: int, bar_t, balance: float, conn: MT5Connector,
                primary_slot=None) -> float:
    """
    Process a completed M15 bar for `slot`. Manages position, detects new setups.
    Returns updated balance.
    """
    instr = slot.key
    SYM   = slot.symbol
    PS    = slot.pip_size
    PV    = slot.pip_val
    SLIP  = slot.slippage

    bar      = bars.iloc[-2]
    bar_h    = float(bar["high"])
    bar_l    = float(bar["low"])
    bar_c    = float(bar["close"])
    bar_hour = bar_t.hour
    bar_date = bar_t.date()

    atr_window = bars.iloc[:-1]
    atr     = _atr(atr_window, ATR_PERIOD)
    atr_avg = _atr(atr_window, min(ATR_AVG_PERIOD, len(atr_window)))

    # ── MT5 position reconciliation ───────────────────────────────────────────
    # Prevents duplicate entries after instant-close bug clears slot.position
    # while MT5 still has open positions with our magic number.
    _real_pos = mt5.positions_get(symbol=SYM) or []
    _our_pos  = [p for p in _real_pos if p.magic == slot.magic]
    _tracked  = slot.position.get("ticket") if slot.position else None

    # Case 1: tracked ticket vanished from MT5 (position closed without us seeing it)
    if slot.position is not None and _tracked not in [p.ticket for p in _our_pos]:
        LOG.warning(f"RECONCILE [{instr}] ticket {_tracked} gone from MT5 — clearing state (instant-close or manual)")
        slot.position = None
        slot.save()
        return balance  # skip this bar; enter fresh next bar

    # Case 2: no tracked position but MT5 has positions with our magic → orphaned
    if slot.position is None and _our_pos:
        tick = mt5.symbol_info_tick(SYM)
        to_adopt = []
        for op in _our_pos:
            direction = "long" if op.type == 0 else "short"
            cur  = tick.bid if direction == "long" else tick.ask
            pnl  = round(((cur - op.price_open) if direction == "long" else (op.price_open - cur)) / PS * PV * op.volume, 2)
            if pnl > 0:
                # Profitable orphan → close and take the money
                ok, _ = conn.close_position(op.ticket)
                if ok:
                    balance += pnl
                    slot.daily_pnl += pnl
                    LOG.warning(f"RECONCILE [{instr}] closed PROFITABLE orphan ticket={op.ticket} "
                                f"{'long' if op.type==0 else 'short'} @ {op.price_open:.2f} | +{pnl:.2f}")
            else:
                to_adopt.append((op, pnl))

        if to_adopt:
            # Among losing orphans: adopt the one with smallest loss (best chance of recovery)
            to_adopt.sort(key=lambda x: x[1], reverse=True)
            adopt, adopt_pnl = to_adopt[0]
            for extra, _ in to_adopt[1:]:
                conn.close_position(extra.ticket)
                LOG.warning(f"RECONCILE [{instr}] closed extra losing orphan ticket={extra.ticket}")
            # Reconstruct position state from MT5 data
            direction = "long" if adopt.type == 0 else "short"
            entry = float(adopt.price_open)
            sl2   = float(adopt.sl) if adopt.sl > 0 else compute_sl(entry, direction, atr, None)
            tp1_2 = _tp1_price(entry, sl2, direction, slot.tp1_rr)
            slot.position = {
                "ticket": adopt.ticket, "direction": direction,
                "entry_price": entry, "sl": sl2, "tp1": tp1_2, "current_sl": sl2,
                "lot": float(adopt.volume), "half_lot": max(0.01, round(adopt.volume * 0.5, 2)),
                "open_bar": bar_index, "tp1_reached": False, "tp1_pnl": 0.0,
                "pattern": "recovered", "atr_at_entry": atr,
                "entry_time": datetime.utcfromtimestamp(adopt.time).isoformat(),
                "d1_regime": slot.d1_regime, "h4_dir": slot.h4_dir, "daily_bias": slot.daily_bias,
                "session": "any", "balance_before": balance, "sl_distance": abs(entry - sl2),
                "best_price": entry, "trail_moves": 0,
            }
            slot.save()
            LOG.warning(f"RECONCILE [{instr}] ADOPTED orphan ticket={adopt.ticket} "
                        f"{direction.upper()} @ {entry:.2f} SL={sl2:.2f} TP1={tp1_2:.2f} "
                        f"(pnl={adopt_pnl:+.2f}) — managing to exit")
        return balance

    # Case 3: tracked position exists but there are extra duplicates → close them
    if slot.position is not None and len(_our_pos) > 1:
        for extra in _our_pos:
            if extra.ticket != _tracked:
                LOG.error(f"RECONCILE [{instr}] duplicate ticket {extra.ticket} @ {extra.price_open:.2f} — auto-closing")
                conn.close_position(extra.ticket)
    # ─────────────────────────────────────────────────────────────────────────

    LOG.debug(f"BAR[{bar_index}] {str(bar_t)[:16]} [{instr}] | "
              f"O={bar['open']:.2f} H={bar_h:.2f} L={bar_l:.2f} C={bar_c:.2f} | "
              f"ATR={atr:.2f} | D1={slot.d1_regime} H4={slot.h4_dir} bias={slot.daily_bias}")

    # Daily reset
    if bar_date != slot.daily_date:
        slot.daily_date = bar_date
        slot.daily_pnl  = 0.0
        LOG.info(f"--- New day [{instr}]: {bar_date} ---")

    # Startup context init (first bar)
    if not slot._startup:
        slot._startup = True
        init = bars.iloc[:-1]
        d1, h4 = build_htf(init)
        d1b = d1[d1.index.date < bar_date]
        if len(d1b) >= 12:
            slot.d1_regime = compute_d1_regime(d1b)
        if len(d1b) >= 2:
            slot.pdh  = float(d1b.iloc[-1]["high"])
            slot.pdl  = float(d1b.iloc[-1]["low"])
            slot.pdc  = float(d1b.iloc[-1]["close"])
            rec       = d1b.iloc[-min(20, len(d1b)):]
            slot.d1rh = float(rec["high"].max())
            slot.d1rl = float(rec["low"].min())
        h4cut = pd.Timestamp(year=bar_date.year, month=bar_date.month,
                              day=bar_date.day, hour=bar_hour, tzinfo=timezone.utc)
        h4b = h4[h4.index + pd.Timedelta(hours=4) <= h4cut]
        if len(h4b) >= 6:
            slot.h4_dir = compute_h4_direction(h4b)
        slot.daily_bias, _ = compute_daily_bias(
            slot.d1_regime, slot.h4_dir, bar_c,
            slot.d1rh, slot.d1rl, slot.pdh, slot.pdl, slot.pdc)
        # Asian / prior-day range from history
        _ar = init[(init.index.hour >= 22) | (init.index.hour < 7)]
        if len(_ar) >= 4:
            slot.asian_h = float(_ar["high"].max())
            slot.asian_l = float(_ar["low"].min())
        slot.last_ctx = bar_date
        LOG.info(f"STARTUP [{instr}] | D1={slot.d1_regime} H4={slot.h4_dir} bias={slot.daily_bias}")

    # Asian range build (GOLD) or prior 24h (BTC)
    ctx_hour = 7 if slot.weekdays_only else 0
    if slot.weekdays_only:
        if bar_hour >= 22 or bar_hour < 7:
            slot.ab_h = max(slot.ab_h, bar_h); slot.ab_l = min(slot.ab_l, bar_l)

    # Daily context update
    if bar_hour == ctx_hour and bar_date != slot.last_ctx:
        slot.last_ctx = bar_date
        if slot.weekdays_only:
            if slot.ab_h > slot.ab_l and slot.ab_h != -1e18:
                slot.asian_h = slot.ab_h; slot.asian_l = slot.ab_l
            slot.ab_h = -1e18; slot.ab_l = 1e18
        else:
            # BTC: use prior 96 bars (24h) as reference range
            prev = bars.iloc[max(0, len(bars)-97):-1]
            if len(prev) >= 10:
                slot.asian_h = float(prev["high"].max())
                slot.asian_l = float(prev["low"].min())

        d1_all, h4_all = build_htf(bars.iloc[:-1])
        d1b = d1_all[d1_all.index.date < bar_date]
        if len(d1b) >= 2:
            slot.pdh  = float(d1b.iloc[-1]["high"])
            slot.pdl  = float(d1b.iloc[-1]["low"])
            slot.pdc  = float(d1b.iloc[-1]["close"])
            rec       = d1b.iloc[-min(20, len(d1b)):]
            slot.d1rh = float(rec["high"].max())
            slot.d1rl = float(rec["low"].min())
        if len(d1b) >= 12:
            slot.d1_regime = compute_d1_regime(d1b)
        h4cut = pd.Timestamp(year=bar_date.year, month=bar_date.month,
                              day=bar_date.day, hour=ctx_hour, tzinfo=timezone.utc)
        h4b = h4_all[h4_all.index + pd.Timedelta(hours=4) <= h4cut]
        if len(h4b) >= 12:
            slot.h4_dir = compute_h4_direction(h4b)
        if slot.d1rh is not None:
            slot.daily_bias, _ = compute_daily_bias(
                slot.d1_regime, slot.h4_dir, bar_c,
                slot.d1rh, slot.d1rl, slot.pdh, slot.pdl, slot.pdc)
        LOG.info(f"DAILY CONTEXT [{instr}] | {bar_date} | D1={slot.d1_regime} H4={slot.h4_dir} bias={slot.daily_bias}")

    # ── Manage open position ──────────────────────────────────────────────────
    if slot.position is not None:
        pos       = slot.position
        direction = pos["direction"]
        bars_held = bar_index - pos["open_bar"]

        old_best = pos.get("best_price", pos["entry_price"])
        pos["best_price"] = max(old_best, bar_h) if direction == "long" else min(old_best, bar_l)

        sl_hit  = check_sl_hit(bar_l, bar_h, pos["current_sl"], direction)
        tp1_hit = not pos["tp1_reached"] and check_tp1_hit(bar_l, bar_h, pos["tp1"], direction)

        # ── 0.8R breakeven lock: bar traded 80% of risk in our favour ─────────
        if not pos.get("tp1_reached", False) and not pos.get("be_locked", False):
            _risk     = abs(pos["entry_price"] - pos["sl"])
            _best_bar = bar_l if direction == "short" else bar_h
            _profit   = (pos["entry_price"] - _best_bar) if direction == "short" else (_best_bar - pos["entry_price"])
            if _profit >= 0.8 * _risk:
                _be = round(pos["entry_price"], 2)
                _ok = (_be < pos["current_sl"]) if direction == "short" else (_be > pos["current_sl"])
                if _ok:
                    pos["current_sl"] = _be
                    pos["be_locked"]  = True
                    slot.position = pos; slot.save()
                    conn.modify_sl(pos["ticket"], _be)
                    LOG.info(f"BE LOCK [{instr}] 0.8R profit reached — SL→entry @ {_be:.2f}")

        if sl_hit:
            fill      = pos["current_sl"] - SLIP if direction == "long" else pos["current_sl"] + SLIP
            remaining = pos["half_lot"] if pos["tp1_reached"] else pos["lot"]
            ok, _     = conn.close_position(pos["ticket"], magic=slot.magic)
            if ok:
                pnl        = _pnl_usd(direction, pos["entry_price"], fill, remaining, PS, PV)
                pnl       += pos.get("tp1_pnl", 0.0)
                result_str = "win" if pnl > 0 else "loss"
                balance   += pnl; slot.daily_pnl += pnl
                slot.consec_losses = 0 if result_str == "win" else slot.consec_losses + 1
                _log_trade(instr, pos, round(fill, 2), "SL_HIT", pnl, bars_held, result_str, balance)
                slot.position = None; slot.save()
            return balance

        elif tp1_hit:
            tp1_pnl = _pnl_usd(direction, pos["entry_price"], pos["tp1"], pos["half_lot"], PS, PV)
            ok = slot.partial_close(pos["ticket"], pos["half_lot"], direction)
            if ok:
                # R4: breakeven with ATR buffer to absorb brief pullbacks (BTC=0.25×, GOLD=0.0)
                buf = slot.be_buffer_atr * atr
                if direction == "long":
                    be_sl = round(pos["entry_price"] - buf, 2)
                else:
                    be_sl = round(pos["entry_price"] + buf, 2)
                pos["tp1_reached"] = True; pos["tp1_pnl"] = tp1_pnl
                pos["current_sl"]  = be_sl
                slot.position = pos; slot.save()
                conn.modify_sl(pos["ticket"], be_sl)
                buf_str = f" (buf={buf:.2f})" if buf > 0 else ""
                LOG.info(f"TP1 HIT [{instr}] | {direction.upper()} +{tp1_pnl:.2f} | SL->BE{buf_str} @ {be_sl:.2f}")

        elif pos["tp1_reached"]:
            m15_slice = bars.iloc[max(0, len(bars)-32):-1]
            new_sl = update_trail_sl(pos, m15_slice, atr, current_price=pos["best_price"])
            if new_sl != pos["current_sl"]:
                old_sl = pos["current_sl"]
                pos["current_sl"] = new_sl; pos["trail_moves"] = pos.get("trail_moves", 0) + 1
                slot.position = pos; slot.save()
                conn.modify_sl(pos["ticket"], round(new_sl, 2))
                LOG.info(f"TRAIL [{instr}] | {old_sl:.2f} -> {new_sl:.2f} ATR={atr:.2f}")
            if check_regime_exit(pos, slot.d1_regime, slot.h4_dir):
                tick  = mt5.symbol_info_tick(SYM)
                fill  = tick.bid if direction == "long" else tick.ask
                ok, _ = conn.close_position(pos["ticket"], magic=slot.magic)
                if ok:
                    pnl  = (_pnl_usd(direction, pos["entry_price"], fill, pos["half_lot"], PS, PV)
                             + pos["tp1_pnl"])
                    result_str = "win" if pnl > 0 else "loss"
                    balance += pnl; slot.daily_pnl += pnl
                    slot.consec_losses = 0 if result_str == "win" else slot.consec_losses + 1
                    _log_trade(instr, pos, round(fill, 2), "REGIME_EXIT", pnl, bars_held, result_str, balance)
                    slot.position = None; slot.save()
                return balance

        elif check_time_stop(bars_held, pos["tp1_reached"]):
            tick  = mt5.symbol_info_tick(SYM)
            fill  = tick.bid if direction == "long" else tick.ask
            ok, _ = conn.close_position(pos["ticket"], magic=slot.magic)
            if ok:
                pnl  = _pnl_usd(direction, pos["entry_price"], fill, pos["lot"], PS, PV)
                result_str = "win" if pnl > 0 else "loss"
                balance += pnl; slot.daily_pnl += pnl
                slot.consec_losses = 0 if result_str == "win" else slot.consec_losses + 1
                _log_trade(instr, pos, round(fill, 2), "TIME_STOP", pnl, bars_held, result_str, balance)
                slot.position = None; slot.save()
            return balance

        slot.save()

    # ── Handle pending Pattern A limit order ──────────────────────────────────
    if slot.pending is not None and slot.position is None:
        p = slot.pending
        bars_since = bar_index - p["created_bar"]
        if bars_since > PATTERN_A_EXPIRE:
            if p.get("order_ticket"):
                req = {"action": mt5.TRADE_ACTION_REMOVE, "order": p["order_ticket"]}
                mt5.order_send(req)
            LOG.info(f"PENDING A expired [{instr}]")
            slot.pending = None
        else:
            orders = mt5.orders_get(ticket=p.get("order_ticket", 0))
            if not orders:
                positions = mt5.positions_get(symbol=SYM) or []
                for mp in positions:
                    if mp.magic == slot.magic:
                        fill  = mp.price_open
                        sl2   = compute_sl(fill, p["direction"], p["atr"], p.get("sweep_extreme"))
                        tp1_2 = _tp1_price(fill, sl2, p["direction"], slot.tp1_rr)
                        conn.modify_sl(mp.ticket, round(sl2, 2))
                        slot.position = {
                            "ticket": mp.ticket, "direction": p["direction"],
                            "entry_price": fill, "sl": sl2, "tp1": tp1_2, "current_sl": sl2,
                            "lot": p["lot"], "half_lot": max(0.01, round(p["lot"]*0.5, 2)),
                            "open_bar": bar_index, "tp1_reached": False, "tp1_pnl": 0.0,
                            "pattern": "A", "atr_at_entry": p["atr"],
                            "entry_time": bar_t.isoformat(),
                            "d1_regime": slot.d1_regime, "h4_dir": slot.h4_dir,
                            "daily_bias": slot.daily_bias, "session": p.get("session", "?"),
                            "balance_before": balance, "sl_distance": abs(fill - sl2),
                            "best_price": fill, "trail_moves": 0,
                        }
                        slot.pending = None; slot.save()
                        LOG.info(f"ENTRY A FILLED [{instr}] | {p['direction'].upper()} @ {fill:.2f} "
                                 f"SL={sl2:.2f} TP1={tp1_2:.2f}")
                        break

    # ── Pattern detection ─────────────────────────────────────────────────────
    _bar_t_iso = bar_t.isoformat() if hasattr(bar_t, "isoformat") else str(bar_t)
    # Secondary slot (slot_idx>0) only enters while primary already has an active trade
    _primary_busy = (slot.slot_idx == 0) or (primary_slot is not None and primary_slot.position is not None)
    if (_primary_busy
            and slot.position is None and slot.pending is None
            and bar_index != slot.last_entry_bar
            and _bar_t_iso != slot.last_entry_bar_time):
        # Guard checks
        if slot.daily_pnl < -(balance * slot.daily_loss_lim_pct):
            LOG.info(f"SKIP [{instr}] daily loss limit hit: {slot.daily_pnl:.2f} (limit {slot.daily_loss_lim_pct*100:.1f}%)")
        elif slot.consec_losses >= slot.consec_halt:
            if slot.halt_cooldown_bar == 0:
                slot.halt_cooldown_bar = bar_index + HALT_COOLDOWN
                LOG.info(f"HALT [{instr}] | {slot.consec_losses} consec losses | cooldown {HALT_COOLDOWN} bars")
            if bar_index >= slot.halt_cooldown_bar:
                slot.consec_losses = 0 if slot.halt_reset_zero else CONSEC_REDUCE
                slot.halt_cooldown_bar = 0
                LOG.info(f"HALT LIFTED [{instr}] | consec reset to {slot.consec_losses}")
            else:
                bars_left = slot.halt_cooldown_bar - bar_index
                LOG.debug(f"SKIP [{instr}] HALT cooldown ({bars_left} bars remaining)")
        elif slot.d1_regime in (D1_CHOP, D1_COMPRESS):
            LOG.info(f"SKIP [{instr}] regime={slot.d1_regime} (no new entries in chop/compress)")
        elif atr_avg > 0 and (atr > 3.0 * atr_avg or atr < slot.atr_low_factor * atr_avg):
            LOG.debug(f"SKIP [{instr}] ATR out of range: {atr:.2f} (avg={atr_avg:.2f}, range={slot.atr_low_factor:.1f}x–3.0x)")
        else:
            tick   = mt5.symbol_info_tick(SYM)
            spread = (tick.ask - tick.bid) / PS
            if spread > slot.spread_max:
                LOG.debug(f"SKIP [{instr}] spread={spread:.1f} > {slot.spread_max}")
            else:
                risk_pct  = slot.risk_pct_reduced if slot.consec_losses >= CONSEC_REDUCE else slot.risk_pct
                m15_slice = bars.iloc[:-1]
                setups    = []

                for direction in ("long", "short"):
                    if not bias_allows_direction(slot.daily_bias, direction):
                        continue
                    if slot.regime_dir_filter:
                        if slot.d1_regime == D1_BULL and direction == "short":
                            LOG.debug(f"SKIP [{instr}] regime_dir_filter: BULL regime, skipping short")
                            continue
                        if slot.d1_regime == D1_BEAR and direction == "long":
                            LOG.debug(f"SKIP [{instr}] regime_dir_filter: BEAR regime, skipping long")
                            continue
                    if not slot.pattern_b_only:
                        sc = detect_pattern_c(m15_slice, direction, atr)
                        if sc: setups.append(sc)
                    sa = detect_pattern_a(m15_slice, bar_hour, direction,
                                          slot.asian_h, slot.asian_l, slot.pdh, slot.pdl, atr)
                    if sa: setups.append(sa)
                    sb = detect_pattern_b(m15_slice, direction, atr)
                    if sb: setups.append(sb)

                if not setups:
                    _scanned = [d for d in ("long","short") if bias_allows_direction(slot.daily_bias, d)]
                    _diag = " || ".join(f"{d}: {scan_diagnostic(m15_slice, d, atr)}" for d in _scanned)
                    LOG.debug(f"SCAN [{instr}] no patterns | spread={spread:.1f} | {_diag}")

                best = pattern_priority(setups)
                if best is not None:
                    direction   = best["direction"]
                    entry_price = float(best["entry_price"])
                    sl  = compute_sl(entry_price, direction, atr, best.get("sweep_extreme"))
                    tp1 = _tp1_price(entry_price, sl, direction, slot.tp1_rr)
                    if not sl_distance_valid(entry_price, sl, atr):
                        best = None

                if best is not None:
                    # Pre-order validation: reject stale entry and check min stop distance
                    _tick_live = mt5.symbol_info_tick(SYM)
                    _live_px   = _tick_live.ask if direction == "long" else _tick_live.bid
                    if abs(_live_px - entry_price) > 0.5 * atr:
                        LOG.info(f"SKIP [{instr}] stale entry: bar={entry_price:.2f} "
                                 f"live={_live_px:.2f} drift={abs(_live_px - entry_price):.1f} "
                                 f"limit=0.5×ATR={0.5*atr:.1f}")
                        best = None
                    if best is not None:
                        _sym_info = mt5.symbol_info(SYM)
                        if _sym_info:
                            _min_stop = _sym_info.trade_stops_level * _sym_info.point
                            if _min_stop > 0 and abs(_live_px - sl) < _min_stop:
                                LOG.info(f"SKIP [{instr}] SL within stops_level: "
                                         f"gap={abs(_live_px - sl):.2f} < min={_min_stop:.2f}")
                                best = None

                if best is not None:
                    lot   = compute_lot(balance, abs(entry_price - sl), risk_pct)
                    ptype = best["type"]
                    LOG.info(f"SETUP [{instr}] Pattern {ptype} {direction.upper()} | "
                             f"entry={entry_price:.2f} SL={sl:.2f} TP1={tp1:.2f} | "
                             f"lot={lot:.2f} risk={risk_pct*100:.1f}% ATR={atr:.2f}")

                    if ptype == "A":
                        order_type = mt5.ORDER_TYPE_BUY_LIMIT if direction == "long" \
                                     else mt5.ORDER_TYPE_SELL_LIMIT
                        far_tp = round(entry_price + 500*PS if direction == "long"
                                       else entry_price - 500*PS, 2)
                        req = {
                            "action": mt5.TRADE_ACTION_PENDING, "symbol": SYM,
                            "volume": round(lot, 2), "type": order_type,
                            "price": round(entry_price, 2), "sl": round(sl, 2), "tp": far_tp,
                            "deviation": 20, "magic": slot.magic, "comment": f"H-A-{instr}",
                            "type_time": mt5.ORDER_TIME_GTC,
                            "type_filling": mt5.ORDER_FILLING_RETURN,
                        }
                        r = mt5.order_send(req)
                        if r.retcode == mt5.TRADE_RETCODE_DONE:
                            slot.pending = {
                                "type": "A", "direction": direction,
                                "entry_price": entry_price, "lot": lot, "atr": atr,
                                "sl": sl, "tp1": tp1,
                                "sweep_extreme": best.get("sweep_extreme"),
                                "session": best.get("session", "?"),
                                "created_bar": bar_index, "order_ticket": r.order,
                            }
                    else:
                        far_tp = round(entry_price + 500*PS if direction == "long"
                                       else entry_price - 500*PS, 2)
                        ok, r = conn.place_order("buy" if direction == "long" else "sell",
                                                  lot, sl, far_tp, symbol=SYM, magic=slot.magic)
                        if ok:
                            slot.last_entry_bar = bar_index
                            slot.last_entry_bar_time = _bar_t_iso
                            slot.save()
                            time.sleep(0.5)
                            positions = mt5.positions_get(symbol=SYM) or []
                            matched = None
                            for mp in positions:
                                if mp.magic == slot.magic:
                                    matched = mp
                                    break

                            if matched:
                                fill  = matched.price_open
                                sl2   = compute_sl(fill, direction, atr, best.get("sweep_extreme"))
                                tp1_2 = _tp1_price(fill, sl2, direction, slot.tp1_rr)
                                conn.modify_sl(matched.ticket, round(sl2, 2))
                                slot.position = {
                                    "ticket": matched.ticket, "direction": direction,
                                    "entry_price": fill, "sl": sl2, "tp1": tp1_2,
                                    "current_sl": sl2, "lot": lot,
                                    "half_lot": max(0.01, round(lot*0.5, 2)),
                                    "open_bar": bar_index, "tp1_reached": False, "tp1_pnl": 0.0,
                                    "pattern": ptype, "atr_at_entry": atr,
                                    "entry_time": bar_t.isoformat(),
                                    "d1_regime": slot.d1_regime, "h4_dir": slot.h4_dir,
                                    "daily_bias": slot.daily_bias,
                                    "session": best.get("session", "?"),
                                    "balance_before": balance,
                                    "sl_distance": abs(fill - sl2),
                                    "best_price": fill, "trail_moves": 0,
                                }
                                slot.save()
                                LOG.info(f"ENTRY [{instr}] Pattern {ptype} {direction.upper()} @ {fill:.2f} "
                                         f"SL={sl2:.2f} TP1={tp1_2:.2f} lot={lot:.2f} ticket={matched.ticket}")
                            else:
                                # Position already closed (instant SL hit) — recover from deal history
                                from datetime import datetime as _dt, timedelta as _td
                                frm = _dt.utcnow() - _td(minutes=5)
                                deals = mt5.history_deals_get(frm, _dt.utcnow()) or []
                                entry_deal = exit_deal = None
                                for d in deals:
                                    if d.symbol == SYM and d.magic == slot.magic:
                                        if d.entry == 0:   # ENTRY deal
                                            entry_deal = d
                                        elif d.entry == 1: # EXIT deal
                                            exit_deal = d
                                if entry_deal and exit_deal:
                                    fill     = entry_deal.price
                                    ex_price = exit_deal.price
                                    pnl_tot  = exit_deal.profit
                                    sl2      = compute_sl(fill, direction, atr, best.get("sweep_extreme"))
                                    tp1_2    = _tp1_price(fill, sl2, direction, slot.tp1_rr)
                                    result   = "win" if pnl_tot > 0 else "loss"
                                    bal_after = balance + pnl_tot
                                    row = {
                                        "open_time":       bar_t.isoformat(),
                                        "close_time":      exit_deal.time,
                                        "direction":       direction,
                                        "pattern":         ptype,
                                        "session":         best.get("session", "?"),
                                        "entry_price":     fill,
                                        "sl":              sl2,
                                        "tp1":             tp1_2,
                                        "exit_price":      ex_price,
                                        "lot":             lot,
                                        "sl_distance":     abs(fill - sl2),
                                        "pnl":             round(pnl_tot, 2),
                                        "pnl_r":           round(pnl_tot / (lot * abs(fill - sl2) * PV / PS), 2)
                                                           if (lot * abs(fill - sl2)) > 0 else 0,
                                        "result":          result,
                                        "exit_reason":     "SL Hit (instant)",
                                        "d1_regime":       slot.d1_regime,
                                        "h4_dir":          slot.h4_dir,
                                        "daily_bias":      slot.daily_bias,
                                        "atr_at_entry":    atr,
                                        "bars_held":       0,
                                        "tp1_reached":     False,
                                        "tp1_pnl":         0.0,
                                        "balance_before":  balance,
                                        "balance_after":   bal_after,
                                        "best_price_seen": fill,
                                        "trail_moves":     0,
                                        "symbol":          SYM,
                                    }
                                    append_trade(os.path.join(DATA_DIR, INSTRUMENTS[instr]["trades_csv"]), row)
                                    LOG.warning(f"INSTANT-CLOSE [{instr}] Pattern {ptype} {direction.upper()} "
                                                f"fill={fill:.2f} exit={ex_price:.2f} pnl={pnl_tot:+.2f} "
                                                f"({result.upper()}) — recovered from deal history")
                                else:
                                    LOG.warning(f"POSITION LOST [{instr}] — order placed but position "
                                                f"not found in positions or deal history. Check MT5.")
                        else:
                            LOG.error(f"ORDER FAILED [{instr}] rc={getattr(r,'retcode','?')}")

    return balance


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    LOG.info("=" * 65)
    LOG.info("STRATEGY H XM  --  GOLD (weekdays) + BTCUSD (24/7 all week)")
    LOG.info(f"  Account: {cfg.LOGIN}  Server: {cfg.SERVER}")
    LOG.info("=" * 65)

    conn = MT5Connector(login=cfg.LOGIN, password=cfg.PASSWORD,
                        server=cfg.SERVER, symbol="GOLD", path=XM_TERMINAL)
    acct = conn.connect()

    if not conn.is_demo():
        LOG.error("NOT A DEMO ACCOUNT -- refusing to start.")
        conn.shutdown(); return

    mt5.symbol_select("GOLD", True)
    mt5.symbol_select("BTCUSD", True)

    balance = float(acct.balance)
    LOG.info(f"Connected | balance=${balance:.2f} {acct.currency} | leverage 1:{acct.leverage}")

    # Create per-instrument slots — 2 per instrument for scale-in trades
    slots = {k: [InstrumentSlot(k, 0), InstrumentSlot(k, 1)] for k in INSTRUMENTS}

    LOG.info("Live loop started (GOLD weekdays 06-19 UTC | BTC 24/7). Ctrl+C to stop.")

    while True:
        try:
            loop_start = time.time()
            now_utc    = datetime.now(timezone.utc)

            acct_info = mt5.account_info()
            balance   = float(acct_info.balance)
            equity    = float(acct_info.equity)

            any_new_bar = False

            for instr, slot_pair in slots.items():
                primary = slot_pair[0]
                SYM     = primary.symbol

                # Session gate (same for both slots of this instrument)
                if not primary.in_session(now_utc):
                    continue

                # Fetch bars once per instrument
                try:
                    bars = conn.get_candles("M15", count=700, symbol=SYM)
                    if bars.index.tzinfo is None:
                        bars.index = bars.index.tz_localize("UTC")
                except Exception as e:
                    LOG.error(f"FETCH ERROR [{instr}]: {e}")
                    continue

                if len(bars) < 50:
                    continue

                last_bar_time = bars.index[-1]

                for s_idx, slot in enumerate(slot_pair):
                    if last_bar_time == slot.prev_bar:
                        continue   # no new bar for this slot

                    slot.prev_bar  = last_bar_time
                    slot.bar_index += 1
                    any_new_bar    = True

                    psrc = slot_pair[0] if s_idx > 0 else None
                    balance = process_bar(slot, bars, slot.bar_index,
                                          bars.index[-2], balance, conn,
                                          primary_slot=psrc)

            # Write combined status
            status = {
                "broker":    cfg.SERVER,
                "balance":   round(balance, 2),
                "equity":    round(equity, 2),
                "updated":   now_utc.strftime("%Y-%m-%d %H:%M UTC"),
                "utc_hour":  now_utc.hour,
                "instruments": {},
            }
            def _pos_info(s):
                if s.position is None:
                    return None
                pos  = s.position
                tick = mt5.symbol_info_tick(s.symbol)
                cur  = tick.bid if pos["direction"] == "long" else tick.ask
                open_pnl = _pnl_usd(pos["direction"], pos["entry_price"], cur,
                                     pos["half_lot"] if pos["tp1_reached"] else pos["lot"],
                                     s.pip_size, s.pip_val) + pos.get("tp1_pnl", 0.0)
                return {
                    "direction": pos["direction"], "pattern": pos.get("pattern"),
                    "entry": pos["entry_price"], "sl": pos["current_sl"], "tp1": pos["tp1"],
                    "bars_held": s.bar_index - pos["open_bar"],
                    "tp1_reached": pos["tp1_reached"], "open_pnl": round(open_pnl, 2),
                }

            for instr, slot_pair in slots.items():
                slot = slot_pair[0]   # primary: source of regime/session/bias data
                status["instruments"][instr] = {
                    "symbol":        slot.symbol,
                    "in_session":    slot.in_session(now_utc),
                    "d1_regime":     slot.d1_regime,
                    "h4_dir":        slot.h4_dir,
                    "daily_bias":    slot.daily_bias,
                    "daily_pnl":     round(sum(s.daily_pnl for s in slot_pair), 2),
                    "consec_losses": slot.consec_losses,
                    "has_position":  any(s.position is not None for s in slot_pair),
                    "position":      _pos_info(slot_pair[0]),
                    "position2":     _pos_info(slot_pair[1]),
                }

            write_status(status)

            elapsed = time.time() - loop_start
            time.sleep(max(1.0, LOOP_SLEEP - elapsed))

        except KeyboardInterrupt:
            LOG.info("Ctrl+C -- shutting down.")
            break
        except Exception as e:
            import traceback
            LOG.error(f"LOOP ERROR: {e}")
            LOG.debug(traceback.format_exc())
            time.sleep(LOOP_SLEEP)

    conn.shutdown()
    LOG.info("XM bot stopped.")


if __name__ == "__main__":
    main()
