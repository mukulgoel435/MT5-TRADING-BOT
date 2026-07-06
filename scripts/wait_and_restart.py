#!/usr/bin/env python3
"""
scripts/wait_and_restart.py
Waits for all Strategy H open positions (GOLD + BTC) to close via SL or TP,
then automatically launches run_live_h_xm.py with the fixed reconciliation code.
"""
import sys, os, time, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import MetaTrader5 as mt5
import config_xm as cfg

XM_TERMINAL    = r"C:\Program Files\XM Global MT5\terminal64.exe"
MAGICS         = {202411, 202412}   # GOLD magic, BTC magic
SYMBOLS        = ["GOLD", "BTCUSD"]
CHECK_INTERVAL = 20  # seconds between checks


def connect():
    ok = mt5.initialize(path=XM_TERMINAL, login=cfg.LOGIN,
                        password=cfg.PASSWORD, server=cfg.SERVER)
    if not ok:
        print(f"[ERROR] MT5 connect failed: {mt5.last_error()}")
        return False
    return True


def open_positions():
    rows = []
    for sym in SYMBOLS:
        pos = mt5.positions_get(symbol=sym) or []
        for p in pos:
            if p.magic in MAGICS:
                direction = "long" if p.type == 0 else "short"
                tick = mt5.symbol_info_tick(sym)
                pip  = 0.1 if sym == "GOLD" else 1.0
                pv   = 10.0 if sym == "GOLD" else 1.0
                cur  = tick.bid if direction == "long" else tick.ask
                pnl  = round(((cur - p.price_open) if direction == "long"
                               else (p.price_open - cur)) / pip * pv * p.volume, 2)
                rows.append({
                    "sym": sym, "ticket": p.ticket,
                    "direction": direction,
                    "entry": p.price_open, "sl": p.sl,
                    "pnl": pnl,
                })
    return rows


def print_status(rows):
    total_pnl = sum(r["pnl"] for r in rows)
    print(f"  [{time.strftime('%H:%M:%S UTC')}] {len(rows)} position(s) open | "
          f"floating P&L: {total_pnl:+.2f}")
    for r in rows:
        marker = "WIN" if r["pnl"] > 0 else "---"
        print(f"    [{marker}] {r['sym']} {r['direction'].upper()} "
              f"@ {r['entry']:.2f}  SL={r['sl']:.2f}  pnl={r['pnl']:+.2f}")


def main():
    print("=" * 60)
    print("  Strategy H — Wait & Auto-Restart")
    print("  Watching for all positions to close via SL or TP...")
    print("=" * 60)

    if not connect():
        sys.exit(1)

    acct = mt5.account_info()
    print(f"Connected | login={acct.login} | balance=${acct.balance:.2f}\n")

    rows = open_positions()
    if not rows:
        print("No open positions found — starting bot immediately.\n")
    else:
        print(f"Found {len(rows)} open position(s):\n")
        print_status(rows)
        print()

    while True:
        rows = open_positions()
        if not rows:
            break
        print_status(rows)
        time.sleep(CHECK_INTERVAL)

    acct = mt5.account_info()
    print(f"\nAll positions closed! balance=${acct.balance:.2f}")
    print("Starting Strategy H bot in 3 seconds...\n")
    mt5.shutdown()
    time.sleep(3)

    bot = os.path.join(os.path.dirname(__file__), "run_live_h_xm.py")
    subprocess.run([sys.executable, bot])


if __name__ == "__main__":
    main()
