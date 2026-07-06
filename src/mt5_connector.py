"""
src/mt5_connector.py
--------------------
All MetaTrader 5 plumbing. Runs on your Windows machine with the MT5 terminal
open and logged into your demo account, and Algo Trading switched ON.

Install:  pip install MetaTrader5 pandas numpy
The MetaTrader5 package is Windows-only and needs the desktop terminal running.
"""

from datetime import datetime, timedelta
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None  # lets the file import anywhere; real use needs the package + Windows


TIMEFRAMES = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


class MT5Connector:
    def __init__(self, login: int, password: str, server: str, symbol: str = "EURUSD",
                 path: str = None):
        self.login = login
        self.password = password
        self.server = server
        self.symbol = symbol
        self.path = path  # optional: full path to terminal64.exe for multi-broker setups

    # ---- connection ----
    def connect(self):
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package not installed. Run: pip install MetaTrader5\n"
                "(Windows only, with the MT5 terminal open.)")
        # If MT5 already initialized (second symbol), just add the symbol
        if mt5.account_info() is not None:
            mt5.symbol_select(self.symbol, True)  # may return False spuriously if already selected
            if mt5.symbol_info_tick(self.symbol) is None:
                raise RuntimeError(f"Symbol '{self.symbol}' not accessible in Market Watch.")
            return mt5.account_info()
        init_kwargs = dict(login=self.login, password=self.password, server=self.server)
        if self.path:
            init_kwargs["path"] = self.path
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(
                f"MT5 initialize failed: {mt5.last_error()}\n"
                "Check: terminal open, Algo Trading ON, and LOGIN/PASSWORD/SERVER exact.")
        mt5.symbol_select(self.symbol, True)  # may return False spuriously if already selected
        if mt5.symbol_info_tick(self.symbol) is None:
            raise RuntimeError(f"Symbol '{self.symbol}' not accessible in Market Watch.")
        info = mt5.account_info()
        mode = "DEMO" if info.trade_mode == 0 else "*** LIVE ***"
        print(f"Connected | {mode} | account {info.login} | "
              f"balance {info.balance} {info.currency} | leverage 1:{info.leverage}")
        return info

    @staticmethod
    def add_symbol(symbol: str) -> bool:
        """Add a symbol to Market Watch without re-initializing MT5."""
        if mt5 is None:
            return False
        mt5.symbol_select(symbol, True)
        return mt5.symbol_info_tick(symbol) is not None

    @staticmethod
    def symbol_pip_value(symbol: str, pip_size: float) -> float:
        """
        Auto-detect pip value per standard lot from MT5 symbol info.
        pip_value = (pip_size / tick_size) × tick_value_per_lot
        """
        if mt5 is None:
            return pip_size * 10
        try:
            info = mt5.symbol_info(symbol)
            if not info:
                return pip_size * 10
            tick_sz  = info.trade_tick_size  if info.trade_tick_size  > 0 else 0.01
            tick_val = info.trade_tick_value if info.trade_tick_value > 0 else 1.0
            return round((pip_size / tick_sz) * tick_val, 4)
        except Exception:
            return pip_size * 10

    def is_demo(self) -> bool:
        return mt5.account_info().trade_mode == 0

    def balance(self) -> float:
        return mt5.account_info().balance

    # ---- market data ----
    def get_candles(self, timeframe: str = "H1", count: int = 500,
                    symbol: str = None) -> pd.DataFrame:
        sym = symbol or self.symbol
        tf = getattr(mt5, TIMEFRAMES[timeframe])
        rates = mt5.copy_rates_from_pos(sym, tf, 0, count)
        if rates is None or len(rates) == 0:
            for fallback in [50_000, 20_000, 10_000, 5_000]:
                if fallback >= count:
                    continue
                rates = mt5.copy_rates_from_pos(sym, tf, 0, fallback)
                if rates is not None and len(rates) > 0:
                    break
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No candle data returned for {sym} {timeframe}. "
                               "Check MT5 is open and symbol is in Market Watch.")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        return df[["open", "high", "low", "close", "tick_volume"]]

    def current_price(self, symbol: str = None):
        sym = symbol or self.symbol
        t = mt5.symbol_info_tick(sym)
        return {"bid": t.bid, "ask": t.ask}

    # ---- positions & history (to "check real demo trades") ----
    def open_positions(self, symbol: str = None):
        sym = symbol or self.symbol
        pos = mt5.positions_get(symbol=sym)
        return list(pos) if pos else []

    def closed_deals(self, days: int = 30) -> pd.DataFrame:
        """Pull closed deal history from the demo account."""
        frm = datetime.now() - timedelta(days=days)
        deals = mt5.history_deals_get(frm, datetime.now())
        if not deals:
            return pd.DataFrame()
        df = pd.DataFrame(list(deals), columns=deals[0]._asdict().keys())
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df[["time", "symbol", "type", "volume", "price", "profit", "comment"]]

    # ---- trading ----
    def place_order(self, side: str, lots: float, sl: float, tp: float,
                    symbol: str = None, magic: int = 202406):
        sym = symbol or self.symbol
        tick = mt5.symbol_info_tick(sym)
        price = tick.ask if side == "buy" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": magic,
            "comment": "py-bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        ok = result.retcode == mt5.TRADE_RETCODE_DONE
        print(f"{'OK' if ok else 'FAIL'} {side} {lots} {sym} @ {price} "
              f"sl={sl:.5f} tp={tp:.5f} magic={magic} retcode={result.retcode}")
        return ok, result

    def close_position(self, ticket: int, magic: int = 202406):
        """Close an open position at market (smart exit / trailing)."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False, None
        pos        = positions[0]
        sym        = pos.symbol          # use the position's actual symbol
        side_close = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
        tick       = mt5.symbol_info_tick(sym)
        price      = tick.bid if pos.type == 0 else tick.ask   # BUY exits at bid, SELL at ask
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       sym,
            "volume":       pos.volume,
            "type":         side_close,
            "position":     ticket,
            "price":        price,
            "deviation":    20,
            "magic":        magic,
            "comment":      "smart-exit",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        return result.retcode == mt5.TRADE_RETCODE_DONE, result

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        """Move SL to new level — used for breakeven lock and trailing stop."""
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        pos = positions[0]
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,      # use the position's actual symbol (not self.symbol)
            "position": ticket,
            "sl":       float(new_sl),
            "tp":       float(pos.tp),
        }
        result = mt5.order_send(request)
        return result.retcode == mt5.TRADE_RETCODE_DONE

    def shutdown(self):
        if mt5 is not None:
            mt5.shutdown()
