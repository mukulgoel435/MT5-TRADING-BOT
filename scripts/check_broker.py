#!/usr/bin/env python3
"""
scripts/check_broker.py  —  Verify new broker connection + symbol availability
==============================================================================
Run after updating config.py with new broker credentials:
    py -3 scripts/check_broker.py

Checks:
  1. MT5 connection works
  2. XAUUSD available and tradeable
  3. BTC symbol found (tries several names)
  4. Whether BTC has weekend M15 bars (key for weekend trading)
  5. Spread and contract specs for both
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone, timedelta
import config

SEP = "-" * 60

def check_symbol(sym):
    info = mt5.symbol_info(sym)
    if info is None:
        return None
    mt5.symbol_select(sym, True)
    return info

def has_weekend_bars(sym):
    """Check if symbol has M15 bars on a recent Saturday."""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=14)
    rates = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M15, start, end)
    if rates is None or len(rates) == 0:
        return False, 0
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df["wd"]   = df["time"].dt.weekday
    wkend = df[df["wd"].isin([5, 6])]
    return len(wkend) > 0, len(wkend)

def main():
    print(f"\n{'='*60}")
    print(f"  BROKER CHECK  |  {config.SERVER}")
    print(f"{'='*60}\n")

    # ── Connect ────────────────────────────────────────────────────────────────
    # XM requires its own MT5 terminal — set path if MetaQuotes terminal is also running
    XM_MT5_PATH = r"C:\Program Files\XM Global MT5\terminal64.exe"   # adjust if needed
    import os
    init_kwargs = dict(login=config.LOGIN, password=config.PASSWORD, server=config.SERVER)
    if os.path.exists(XM_MT5_PATH):
        init_kwargs["path"] = XM_MT5_PATH
        print(f"  Using XM terminal: {XM_MT5_PATH}")

    print(f"  Connecting to {config.SERVER}...", end="", flush=True)
    if not mt5.initialize(**init_kwargs):
        err = mt5.last_error()
        print(f" FAILED\n  Error: {err}")
        print(f"\n  Check: login={config.LOGIN}  server={config.SERVER}")
        return
    info = mt5.account_info()
    print(f" OK")
    print(f"  Account : {info.login}  |  {info.server}")
    print(f"  Balance : ${info.balance:.2f}  |  Currency: {info.currency}")
    print(f"  Leverage: 1:{info.leverage}")

    # ── XAUUSD ────────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  XAUUSD")
    print(SEP)
    si = check_symbol("XAUUSD")
    if si:
        pip = si.point * 10
        pip_val = si.trade_tick_value / si.trade_tick_size * pip
        print(f"  Found      : XAUUSD")
        print(f"  Spread     : {si.spread} pts  (~${si.spread * pip:.2f}/lot)")
        print(f"  Contract   : {si.trade_contract_size} oz / lot")
        print(f"  Min lot    : {si.volume_min}")
        print(f"  Trade mode : {si.trade_mode}  (4=full, 0=disabled)")
        we, we_n = has_weekend_bars("XAUUSD")
        print(f"  Weekend bars (14d): {we_n}  ({'HAS weekend data' if we else 'NO weekend data -- expected for gold'})")
    else:
        print(f"  NOT FOUND on this broker")

    # ── BTC ───────────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  BITCOIN / BTCUSD")
    print(SEP)
    btc_candidates = ["BTCUSD", "Bitcoin", "BTCUSD.", "BTC/USD", "XBTUSD",
                      "BTC", "BTCUSDT", "BTCUSD+", "#BTCUSD"]
    found_btc = None
    for sym in btc_candidates:
        si = check_symbol(sym)
        if si is not None:
            found_btc = (sym, si)
            break

    if found_btc is None:
        print(f"  NOT FOUND with common names. Searching all symbols...")
        all_syms = mt5.symbols_get() or []
        crypto = [s for s in all_syms
                  if any(k in s.name.lower() for k in ("btc","bitcoin","xbt","crypto"))]
        if crypto:
            print(f"  Found {len(crypto)} crypto symbol(s):")
            for s in crypto[:10]:
                print(f"    {s.name}")
            found_btc = (crypto[0].name, mt5.symbol_info(crypto[0].name))
        else:
            print(f"  No BTC/crypto symbols found on this broker.")
            print(f"  => This broker does NOT support BTC trading.")

    if found_btc:
        sym, si = found_btc
        print(f"  Found      : {sym}")
        print(f"  Spread     : {si.spread} pts")
        print(f"  Contract   : {si.trade_contract_size} / lot")
        print(f"  Min lot    : {si.volume_min}")
        print(f"  Trade mode : {si.trade_mode}  (4=full, 0=disabled)")
        print(f"  Checking weekend bars...", end="", flush=True)
        we, we_n = has_weekend_bars(sym)
        print(f" {we_n} bars")
        if we:
            print(f"  [YES] Weekend BTC trading AVAILABLE on {config.SERVER}")
            print(f"  => Can run BTC bot on Sat+Sun when XAUUSD is closed!")
        else:
            print(f"  [NO]  Weekend BTC data NOT available on {config.SERVER}")
            print(f"  => Cannot use for weekend trading. Try a different broker.")

    # ── All available symbols summary ─────────────────────────────────────────
    print(f"\n{SEP}")
    print(f"  Other instruments with weekend bars (checking majors)...")
    print(SEP)
    check_others = ["EURUSD","GBPUSD","USDJPY","ETHUSD","LTCUSD",
                    "XAGUSD","US500","US30","USTEC"]
    for sym in check_others:
        si2 = check_symbol(sym)
        if si2 is None:
            continue
        we2, we_n2 = has_weekend_bars(sym)
        if we2:
            print(f"  {sym:<12} weekend bars={we_n2:>4}  trade_mode={si2.trade_mode}")

    print(f"\n{'='*60}")
    print(f"  Done. Update config.py if broker looks good.")
    print(f"{'='*60}\n")
    mt5.shutdown()

if __name__ == "__main__":
    main()
