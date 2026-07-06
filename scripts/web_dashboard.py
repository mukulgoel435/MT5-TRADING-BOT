"""
scripts/web_dashboard.py  --  Strategy H XM Dashboard (GOLD + BTCUSD)
======================================================================
Pure Python HTTP server, port 5050. No Flask needed.

    py -3 scripts/web_dashboard.py

Open http://localhost:5050  (auto-refreshes every 8 s, all times IST)

Reads:
  data/h_xm_live_status.json   -- XM bot state
  data/h_xm_GOLD_trades.csv    -- GOLD closed trades
  data/h_xm_BTC_trades.csv     -- BTC closed trades
  data/logs/h_xm_YYYY-MM-DD.log
"""

import sys, os, json, csv
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

IST     = timezone(timedelta(hours=5, minutes=30))
PORT    = 5050
BASE    = os.path.join(os.path.dirname(__file__), "..")
DATA    = os.path.join(BASE, "data")
LOG_DIR = os.path.join(DATA, "logs")
XM_STATUS = os.path.join(DATA, "h_xm_live_status.json")
GOLD_CSV  = os.path.join(DATA, "h_xm_GOLD_trades.csv")
BTC_CSV   = os.path.join(DATA, "h_xm_BTC_trades.csv")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ist_now():
    return datetime.now(IST)

def to_ist(dt_str, fmt="%m-%d %H:%M"):
    if not dt_str or str(dt_str).strip() in ("", "None", "—"):
        return "—"
    try:
        s  = str(dt_str)[:19].replace("T", " ")
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime(fmt)
    except Exception:
        return str(dt_str)

def safe_float(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default
    except Exception:
        return default

def load_csv(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []

def load_status():
    if not os.path.exists(XM_STATUS):
        return {}
    try:
        with open(XM_STATUS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_result(row):
    return row.get("result", "").strip().lower()

def get_pnl(row):
    try:
        return float(row.get("pnl", ""))
    except Exception:
        return None


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(trades):
    closed = [t for t in trades if get_result(t) in ("win", "loss")]
    wp  = [get_pnl(t) for t in closed if get_result(t) == "win"  and get_pnl(t) is not None]
    lp  = [get_pnl(t) for t in closed if get_result(t) == "loss" and get_pnl(t) is not None]
    n   = len(closed)
    gw  = sum(wp) if wp else 0.0
    gl  = abs(sum(lp)) if lp else 0.0
    tp1 = sum(1 for t in closed if str(t.get("tp1_reached", "")).lower() == "true")
    return {
        "n":       n,
        "wins":    len(wp),
        "losses":  len(lp),
        "wr":      len(wp) / n * 100 if n > 0 else 0.0,
        "pf":      gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0),
        "net":     round(gw - gl, 2),
        "avg_win": round(gw / len(wp), 2) if wp else 0.0,
        "avg_loss":round(gl / len(lp), 2) if lp else 0.0,
        "gw":      round(gw, 2),
        "gl":      round(gl, 2),
        "tp1_pct": tp1 / n * 100 if n > 0 else 0.0,
    }

def group_stats(trades, key):
    m = defaultdict(list)
    for t in trades:
        m[t.get(key, "?")].append(t)
    return {k: compute_stats(v) for k, v in m.items()}

def max_dd(trades):
    bal = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: str(x.get("open_time", ""))):
        p = get_pnl(t)
        if p is None:
            continue
        bal += p
        if bal > peak:
            peak = bal
        if peak > 0:
            dd = max(dd, (peak - bal) / peak * 100)
    return round(dd, 1)

def recent_log(n=120):
    # Always prefer the most recently modified log file first,
    # then fall back to today's date variants
    candidates = []
    try:
        all_logs = [os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                    if f.startswith("h_xm_") and f.endswith(".log")]
        if all_logs:
            candidates.append(max(all_logs, key=os.path.getmtime))
    except Exception:
        pass
    for tz_offset in (timedelta(hours=5, minutes=30), timedelta(0)):
        d = (datetime.now(timezone.utc) + tz_offset).strftime("%Y-%m-%d")
        p = os.path.join(LOG_DIR, "h_xm_{}.log".format(d))
        if p not in candidates:
            candidates.append(p)

    for log_path in candidates:
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                if lines:
                    return [l.rstrip() for l in lines[-n:]]
            except Exception:
                pass
    return ["(no log file found — bot not started yet)"]


# ── Colour / badge helpers ────────────────────────────────────────────────────

def pnl_clr(v):
    try:
        return "#22c55e" if float(v) >= 0 else "#ef4444"
    except Exception:
        return "#94a3b8"

def pnl_str(v):
    try:
        f = float(v)
        return "+${:.2f}".format(f) if f >= 0 else "-${:.2f}".format(abs(f))
    except Exception:
        return "—"

def regime_clr(r):
    m = {"bull": "#22c55e", "bear": "#ef4444",
         "range": "#60a5fa", "chop": "#f59e0b", "compress": "#f59e0b"}
    return m.get(str(r).lower(), "#94a3b8")

def regime_lbl(r):
    m = {"bull": "BULL", "bear": "BEAR", "range": "RANGE",
         "chop": "CHOP ⛔", "compress": "COMPRESS ⛔"}
    return m.get(str(r).lower(), str(r).upper())

def bias_clr(b):
    m = {"long": "#22c55e", "long_pref": "#86efac",
         "short": "#ef4444", "short_pref": "#fca5a5", "neutral": "#94a3b8"}
    return m.get(str(b).lower(), "#94a3b8")

def bias_lbl(b):
    m = {"long": "LONGS ONLY", "long_pref": "LONG PREF",
         "short": "SHORTS ONLY", "short_pref": "SHORT PREF", "neutral": "NEUTRAL"}
    return m.get(str(b).lower(), str(b).upper())

def wr_clr(wr):
    return "#22c55e" if wr >= 50 else "#ef4444"

def pf_clr(pf):
    return "#22c55e" if pf >= 1.2 else ("#f59e0b" if pf >= 1.0 else "#ef4444")

def dd_clr(d):
    return "#ef4444" if d > 20 else ("#f59e0b" if d > 10 else "#22c55e")

def dir_arrow(d):
    if str(d).lower() == "long":
        return '<span style="color:#22c55e;font-weight:bold">▲ L</span>'
    return '<span style="color:#ef4444;font-weight:bold">▼ S</span>'

def pat_badge(p):
    c = {"A": "#f59e0b", "B": "#60a5fa", "C": "#22c55e"}.get(str(p).upper(), "#64748b")
    return '<span style="color:{};font-weight:bold">P-{}</span>'.format(c, p)

def res_badge(r):
    c = {"win": "#22c55e", "loss": "#ef4444"}.get(str(r).lower(), "#64748b")
    return '<span style="color:{};font-weight:bold">{}</span>'.format(c, str(r).upper())

def sym_badge(sym):
    c = "#f59e0b" if "GOLD" in str(sym).upper() else "#a78bfa"
    return '<span style="color:{};font-weight:bold;font-size:10px">{}</span>'.format(c, sym)

def pct_bar(pct, color="#22c55e"):
    w = min(100, max(0, float(pct)))
    return ('<div style="background:#0f172a;border-radius:3px;height:5px;margin-top:3px">'
            '<div style="background:{};height:5px;border-radius:3px;width:{:.0f}%"></div>'
            '</div>').format(color, w)

def kpi(label, value, color="#e2e8f0", sub=""):
    sub_h = '<div style="font-size:10px;color:#64748b;margin-top:2px">{}</div>'.format(sub) if sub else ""
    return ('<div class="kpi"><div class="lbl">{}</div>'
            '<div class="val" style="color:{}">{}</div>{}</div>').format(label, color, value, sub_h)


# ── Position card ─────────────────────────────────────────────────────────────

def pos_card(pos):
    if not pos:
        return ('<div style="background:#0f172a;border-radius:6px;padding:12px;'
                'color:#334155;font-size:12px;text-align:center">No open position — scanning</div>')
    d        = pos.get("direction", "?")
    op       = safe_float(pos.get("open_pnl", 0))
    tp1r     = pos.get("tp1_reached", False)
    border   = "#22c55e" if d == "long" else "#ef4444"
    dir_text = "▲ LONG" if d == "long" else "▼ SHORT"
    trail_h  = ""
    if tp1r:
        trail_h = ('<div style="color:#60a5fa;font-size:10px;margin-top:6px">'
                   'Trailing SL active | moves: {} | best: {}</div>').format(
                   pos.get("trail_moves", 0), pos.get("best_price", "—"))
    return (
        '<div style="background:#0f172a;border-radius:6px;padding:12px;border-left:4px solid {}">'
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
        '<span style="font-weight:bold;color:{}">{}&nbsp;&nbsp;{}</span>'
        '<span style="color:#64748b;font-size:11px">{} bars held</span></div>'
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">'
        '<div><div class="mlbl">Entry</div><div class="mval">{}</div></div>'
        '<div><div class="mlbl">SL (live)</div><div class="mval" style="color:#ef4444">{}</div></div>'
        '<div><div class="mlbl">TP1</div><div class="mval" style="color:#22c55e">{}</div></div>'
        '<div><div class="mlbl">Open P&amp;L</div><div class="mval" style="color:{}">{}</div></div>'
        '</div>'
        '<div style="margin-top:6px;font-size:10px;color:{}">'
        'TP1 {}</div>{}</div>'
    ).format(
        border, border, dir_text, pat_badge(pos.get("pattern", "?")),
        pos.get("bars_held", 0),
        pos.get("entry", "—"), pos.get("sl", "—"), pos.get("tp1", "—"),
        pnl_clr(op), pnl_str(op),
        "#22c55e" if tp1r else "#475569",
        "✓ hit — trailing half" if tp1r else "not yet reached",
        trail_h
    )


# ── Instrument panel ──────────────────────────────────────────────────────────

def instrument_panel(instr_key, idata, trades, label, sym_color):
    s       = compute_stats(trades)
    pat_s   = group_stats(trades, "pattern")
    dir_s   = group_stats(trades, "direction")
    today   = _ist_now().strftime("%Y-%m-%d")
    today_t = [t for t in trades if to_ist(str(t.get("open_time", "")), "%Y-%m-%d") >= today]
    ts      = compute_stats(today_t)

    regime   = str(idata.get("d1_regime", "—"))
    h4       = str(idata.get("h4_dir",    "—"))
    bias     = str(idata.get("daily_bias","—"))
    dpnl     = idata.get("daily_pnl", 0.0)
    consec   = idata.get("consec_losses", 0)
    in_sess  = idata.get("in_session", False)
    pos      = idata.get("position")
    dd       = max_dd(trades)

    # Build pattern rows (no backslash issues — build as string concat)
    pat_rows = ""
    for p in ("B", "C", "A"):
        ps = pat_s.get(p)
        if not ps or ps["n"] == 0:
            continue
        pc  = {"A": "#f59e0b", "B": "#60a5fa", "C": "#22c55e"}.get(p, "#94a3b8")
        nc  = pnl_clr(ps["net"])
        pfc = pf_clr(ps["pf"])
        pat_rows += (
            "<tr><td style='color:{}'>{}</td>"
            "<td style='text-align:right'>{}</td>"
            "<td style='text-align:right;color:{}'>{:.1f}%</td>"
            "<td style='text-align:right;color:{}'>{:.2f}</td>"
            "<td style='text-align:right;color:{}'>{}</td></tr>"
        ).format(pc, "P-" + p, ps["n"], wr_clr(ps["wr"]), ps["wr"],
                 pfc, ps["pf"], nc, pnl_str(ps["net"]))

    dir_rows = ""
    for d in ("long", "short"):
        ds = dir_s.get(d)
        if not ds or ds["n"] == 0:
            continue
        dc  = "#22c55e" if d == "long" else "#ef4444"
        dl  = "▲ Long" if d == "long" else "▼ Short"
        nc  = pnl_clr(ds["net"])
        pfc = pf_clr(ds["pf"])
        dir_rows += (
            "<tr><td style='color:{}'>{}</td>"
            "<td style='text-align:right'>{}</td>"
            "<td style='text-align:right;color:{}'>{:.1f}%</td>"
            "<td style='text-align:right;color:{}'>{:.2f}</td>"
            "<td style='text-align:right;color:{}'>{}</td></tr>"
        ).format(dc, dl, ds["n"], wr_clr(ds["wr"]), ds["wr"],
                 pfc, ds["pf"], nc, pnl_str(ds["net"]))

    sess_h = ('<span style="color:#22c55e">● ACTIVE</span>' if in_sess
              else '<span style="color:#475569">○ OFF SESSION</span>')
    halt_h = ""
    if consec >= 6:
        halt_h = ' <span style="color:#ef4444;font-weight:bold">⛔ HALTED</span>'
    elif consec >= 3:
        halt_h = ' <span style="color:#f59e0b;font-weight:bold">⚠ REDUCED RISK</span>'

    h4_text = ("▲ BULL" if h4 == "bull" else "▼ BEAR" if h4 == "bear" else "— NONE")

    pat_table = ("—" if not pat_rows else
                 "<table><tr><th>Pat</th><th style='text-align:right'>N</th>"
                 "<th style='text-align:right'>WR%</th><th style='text-align:right'>PF</th>"
                 "<th style='text-align:right'>Net</th></tr>"
                 + pat_rows + "</table>")
    dir_table = ("—" if not dir_rows else
                 "<table><tr><th>Dir</th><th style='text-align:right'>N</th>"
                 "<th style='text-align:right'>WR%</th><th style='text-align:right'>PF</th>"
                 "<th style='text-align:right'>Net</th></tr>"
                 + dir_rows + "</table>")

    return """
<div style="background:#1e293b;border-radius:10px;padding:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <span style="font-size:15px;font-weight:bold;color:{sym_color}">{label}</span>
    <span style="font-size:11px">{sess_h}{halt_h}</span>
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px">
    <div style="background:#0f172a;border-radius:6px;padding:8px 10px">
      <div class="mlbl">D1 Regime</div>
      <div class="mval" style="color:{rc}">{rl}</div>
    </div>
    <div style="background:#0f172a;border-radius:6px;padding:8px 10px">
      <div class="mlbl">H4 Direction</div>
      <div class="mval" style="color:{h4c}">{h4t}</div>
    </div>
    <div style="background:#0f172a;border-radius:6px;padding:8px 10px">
      <div class="mlbl">Daily Bias</div>
      <div class="mval" style="color:{bc}">{bl}</div>
    </div>
  </div>
  <div style="margin-bottom:10px">{pos_h}</div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px">
    {k1}{k2}{k3}{k4}
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:8px">
    {k5}{k6}{k7}{k8}
  </div>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px">
    {k9}{k10}{k11}{k12}
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
    <div><div class="lbl" style="margin-bottom:4px">By Pattern</div>{pat_t}</div>
    <div><div class="lbl" style="margin-bottom:4px">By Direction</div>{dir_t}</div>
  </div>
</div>""".format(
        sym_color=sym_color, label=label, sess_h=sess_h, halt_h=halt_h,
        rc=regime_clr(regime), rl=regime_lbl(regime),
        h4c=regime_clr(h4), h4t=h4_text,
        bc=bias_clr(bias), bl=bias_lbl(bias),
        pos_h=pos_card(pos),
        # Today row
        k1=kpi("Today Trades", str(ts["n"])),
        k2=kpi("Today WR",  "{:.1f}%".format(ts["wr"]) if ts["n"] > 0 else "—",
                wr_clr(ts["wr"]) if ts["n"] > 0 else "#64748b"),
        k3=kpi("Today P&amp;L", pnl_str(dpnl), pnl_clr(dpnl)),
        k4=kpi("Consec Losses", str(consec), "#ef4444" if consec >= 3 else "#e2e8f0"),
        # All-time top row
        k5=kpi("All Trades", str(s["n"])),
        k6=kpi("Win Rate",  "{:.1f}%".format(s["wr"]) if s["n"] > 0 else "—",
                wr_clr(s["wr"]) if s["n"] > 0 else "#64748b"),
        k7=kpi("Profit Factor", "{:.2f}".format(s["pf"]) if s["n"] > 0 else "—",
                pf_clr(s["pf"]) if s["n"] > 0 else "#64748b"),
        k8=kpi("Net P&amp;L", pnl_str(s["net"]) if s["n"] > 0 else "—",
                pnl_clr(s["net"])),
        # Detail row
        k9=kpi("Avg Win",  "${:.2f}".format(s["avg_win"]),  "#22c55e"),
        k10=kpi("Avg Loss", "${:.2f}".format(s["avg_loss"]), "#ef4444"),
        k11=kpi("TP1 Hit %", "{:.1f}%".format(s["tp1_pct"]) if s["n"] > 0 else "—", "#60a5fa"),
        k12=kpi("Max Drawdown", "{:.1f}%".format(dd), dd_clr(dd)),
        pat_t=pat_table, dir_t=dir_table,
    )


# ── Trade rows ────────────────────────────────────────────────────────────────

def trade_rows(trades):
    rows = ""
    display = sorted(trades, key=lambda t: str(t.get("open_time", "")), reverse=True)[:150]
    for t in display:
        pnl  = get_pnl(t)
        res  = get_result(t)
        ot   = to_ist(str(t.get("open_time",  "")))
        ct   = to_ist(str(t.get("close_time", "")))
        ps   = pnl_str(pnl) if pnl is not None else "—"
        pc   = pnl_clr(pnl) if pnl is not None else "#64748b"
        bg   = "#052e16" if res == "win" else ("#2d0c0c" if res == "loss" else "#1e293b")
        tp1r = t.get("tp1_reached", "")
        tp1c = "#22c55e" if str(tp1r).lower() == "true" else "#475569"
        rows += (
            "<tr style='background:{}'>"
            "<td>{}</td><td style='color:#94a3b8;font-size:10px'>{}</td>"
            "<td style='color:#64748b;font-size:10px'>{}</td>"
            "<td>{}</td><td>{}</td>"
            "<td style='font-size:10px'>{}</td><td style='font-size:10px;color:#ef4444'>{}</td>"
            "<td style='font-size:10px;color:#22c55e'>{}</td><td style='font-size:10px'>{}</td>"
            "<td style='font-weight:bold;color:{}'>{}</td>"
            "<td style='color:#94a3b8;font-size:10px'>{}</td>"
            "<td style='font-size:10px;color:#64748b'>{}</td>"
            "<td style='font-size:10px;color:{}'>{}</td>"
            "<td style='font-size:10px;color:#60a5fa'>{}</td>"
            "<td>{}</td>"
            "<td style='font-size:9px;color:#64748b'>{}</td>"
            "<td style='font-size:9px;color:#64748b'>{}</td>"
            "<td style='font-size:9px;color:#64748b'>{}</td>"
            "</tr>"
        ).format(
            bg,
            sym_badge(t.get("symbol", "?")), ot, ct,
            dir_arrow(t.get("direction", "?")), pat_badge(t.get("pattern", "?")),
            t.get("entry_price", "—"), t.get("sl", "—"),
            t.get("tp1", "—"), t.get("exit_price", "—"),
            pc, ps,
            t.get("pnl_r", "—"),
            t.get("bars_held", "—"),
            tp1c, tp1r,
            t.get("trail_moves", "—"),
            res_badge(res),
            t.get("exit_reason", ""),
            t.get("d1_regime", ""),
            t.get("daily_bias", ""),
        )
    if not rows:
        rows = ('<tr><td colspan="18" style="text-align:center;color:#64748b;padding:20px">'
                'No trades yet — bot scanning for setups</td></tr>')
    return rows


# ── Symbol + exit-reason summary tables ──────────────────────────────────────

def sym_table(all_trades):
    rows = ""
    for sym in ("GOLD", "BTCUSD"):
        s = compute_stats([t for t in all_trades if t.get("symbol") == sym])
        if s["n"] == 0:
            continue
        rows += (
            "<tr><td>{}</td>"
            "<td style='text-align:right'>{}</td>"
            "<td style='text-align:right;color:{}'>{:.1f}%</td>"
            "<td style='text-align:right;color:{}'>{:.2f}</td>"
            "<td style='text-align:right;color:#22c55e'>${:.2f}</td>"
            "<td style='text-align:right;color:#ef4444'>${:.2f}</td>"
            "<td style='text-align:right;color:{}'>{}</td></tr>"
        ).format(
            sym_badge(sym), s["n"],
            wr_clr(s["wr"]), s["wr"],
            pf_clr(s["pf"]), s["pf"],
            s["avg_win"], s["avg_loss"],
            pnl_clr(s["net"]), pnl_str(s["net"]),
        )
    return (
        "<table><tr>"
        "<th>Symbol</th><th style='text-align:right'>Trades</th>"
        "<th style='text-align:right'>WR%</th><th style='text-align:right'>PF</th>"
        "<th style='text-align:right'>Avg W</th><th style='text-align:right'>Avg L</th>"
        "<th style='text-align:right'>Net P&amp;L</th></tr>"
        + rows + "</table>"
    ) if rows else "<div style='color:#334155'>No trades yet</div>"


def exit_table(all_trades):
    gs = group_stats(all_trades, "exit_reason")
    rows = ""
    for reason, s in sorted(gs.items(), key=lambda x: -x[1]["n"]):
        if s["n"] == 0:
            continue
        rows += (
            "<tr><td style='color:#94a3b8'>{}</td>"
            "<td style='text-align:right'>{}</td>"
            "<td style='text-align:right;color:{}'>{:.1f}%</td>"
            "<td style='text-align:right;color:{}'>{}</td></tr>"
        ).format(reason, s["n"], wr_clr(s["wr"]), s["wr"],
                 pnl_clr(s["net"]), pnl_str(s["net"]))
    return (
        "<table><tr><th>Exit Reason</th><th style='text-align:right'>N</th>"
        "<th style='text-align:right'>WR%</th><th style='text-align:right'>Net</th></tr>"
        + rows + "</table>"
    ) if rows else "<div style='color:#334155'>No trades yet</div>"


# ── Log HTML ──────────────────────────────────────────────────────────────────

def log_html():
    all_lines = recent_log(120)
    # Show last 5 debug lines for context, then all important (INFO/WARN/ERROR) at bottom
    # Auto-scroll shows the bottom → user sees important events, not debug spam
    important = [l for l in all_lines if "| DEBUG " not in l]
    debug     = [l for l in all_lines if "| DEBUG " in l][-5:]
    lines = (["── last 5 scan lines ──"] + debug + ["── events ──"] + important
             if debug else important)
    parts = []
    for l in lines:
        if "ENTRY" in l or "TP1 HIT" in l or "PARTIAL" in l:
            c = "#22c55e"
        elif "ERROR" in l or "HALT" in l or "POSITION LOST" in l:
            c = "#ef4444"
        elif "RECONCILE" in l or "WARNING" in l:
            c = "#f87171"
        elif "TRAIL" in l or "DAILY CONTEXT" in l or "STARTUP" in l:
            c = "#60a5fa"
        elif "SKIP" in l or "GUARD" in l or "ORDER FAILED" in l:
            c = "#f59e0b"
        elif "SETUP" in l:
            c = "#fde68a"
        elif "scan lines" in l:
            c = "#334155"
        else:
            c = "#94a3b8"
        safe = l.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append('<div class="log-line" style="color:{}">{}</div>'.format(c, safe))
    if not parts:
        parts = ['<div class="log-line" style="color:#334155">(no log lines yet)</div>']
    return "\n".join(parts)


# ── Full page ─────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing:border-box; margin:0; padding:0; }
body { background:#0f172a; color:#e2e8f0; font-family:'Courier New',monospace;
       padding:16px; font-size:13px; }
h1 { color:#f59e0b; font-size:14px; margin-bottom:14px; letter-spacing:1px; }
h3 { color:#64748b; font-size:9px; text-transform:uppercase; letter-spacing:1px;
     margin:16px 0 6px; }
.dot { display:inline-block; width:7px; height:7px; border-radius:50%;
       background:#22c55e; margin-right:7px; animation:pulse 2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
.top { display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
       gap:8px; margin-bottom:14px; }
.kpi { background:#1e293b; border-radius:6px; padding:10px 12px; }
.lbl { font-size:9px; color:#475569; text-transform:uppercase; letter-spacing:.5px; }
.val { font-size:18px; font-weight:bold; margin-top:3px; }
.instruments { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:14px; }
.mlbl { font-size:9px; color:#475569; text-transform:uppercase; }
.mval { font-size:13px; font-weight:bold; margin-top:2px; }
table { width:100%; border-collapse:collapse; font-size:11px; }
th { background:#1e293b; padding:5px 7px; text-align:left; color:#64748b;
     border-bottom:1px solid #334155; font-size:9px; text-transform:uppercase;
     white-space:nowrap; }
td { padding:4px 7px; border-bottom:1px solid #0f172a; white-space:nowrap; }
.log-box { background:#0a1628; border:1px solid #1e293b; border-radius:6px;
           padding:12px; font-size:10px; line-height:1.5; max-height:340px;
           overflow-y:auto; font-family:'Courier New',monospace; }
.log-line { white-space:pre-wrap; word-break:break-all; }
.footer { font-size:10px; color:#334155; margin-top:14px; text-align:center; }
"""

def generate_html():
    status     = load_status()
    gold_t     = load_csv(GOLD_CSV)
    btc_t      = load_csv(BTC_CSV)
    all_t      = ([dict(t, symbol="GOLD")   for t in gold_t] +
                  [dict(t, symbol="BTCUSD") for t in btc_t])

    broker     = status.get("broker", "XM Global MT5")
    balance    = status.get("balance", "—")
    equity     = status.get("equity",  "—")
    upd_utc    = str(status.get("updated", "—"))
    upd_ist    = to_ist(upd_utc.replace(" UTC", ""), "%Y-%m-%d %H:%M IST")

    instrs     = status.get("instruments", {})
    gold_data  = instrs.get("GOLD", {})
    btc_data   = instrs.get("BTC",  {})

    all_s      = compute_stats(all_t)
    today      = _ist_now().strftime("%Y-%m-%d")
    today_t    = [t for t in all_t if to_ist(str(t.get("open_time", "")), "%Y-%m-%d") >= today]
    ts         = compute_stats(today_t)

    gold_panel = instrument_panel("GOLD", gold_data, gold_t, "GOLD (XM)", "#f59e0b")
    btc_panel  = instrument_panel("BTC",  btc_data,  btc_t,  "BTCUSD (XM)", "#a78bfa")

    rows  = trade_rows(all_t)
    s_tbl = sym_table(all_t)
    e_tbl = exit_table(all_t)
    l_html = log_html()
    now_ist = _ist_now().strftime("%Y-%m-%d %H:%M IST")

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="8">
  <title>Strategy H XM — GOLD + BTC</title>
  <style>{css}</style>
</head>
<body>
<h1><span class="dot"></span>Strategy H ISF &nbsp;|&nbsp; XM Global &nbsp;|&nbsp;
  <span style="color:#f59e0b">GOLD</span> + <span style="color:#a78bfa">BTCUSD</span>
  &nbsp;|&nbsp; LIVE DEMO</h1>

<div class="top">
  {b1}{b2}{b3}{b4}{b5}{b6}{b7}{b8}{b9}{b10}
</div>

<div class="instruments">
  {gold_p}
  {btc_p}
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px">
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <h3 style="margin-top:0">Bot Events (INFO/WARN/ERROR — IST)</h3>
    <div class="log-box" id="lb">{log}</div>
  </div>
  <div style="background:#1e293b;border-radius:8px;padding:14px">
    <h3 style="margin-top:0">By Symbol</h3>
    {s_tbl}
    <h3>By Exit Reason</h3>
    {e_tbl}
  </div>
</div>

<h3>Combined Trade History (last 150, newest first — IST)</h3>
<div style="overflow-x:auto">
<table>
  <thead>
    <tr>
      <th>Sym</th><th>Open (IST)</th><th>Close (IST)</th><th>Dir</th><th>Pat</th>
      <th>Entry</th><th>SL</th><th>TP1</th><th>Exit</th>
      <th>P&amp;L</th><th>R</th><th>Bars</th><th>TP1?</th><th>Trail#</th>
      <th>Result</th><th>Exit Reason</th><th>D1</th><th>Bias</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>
</div>

<div class="footer">
  Auto-refresh 8 s &nbsp;|&nbsp; All times IST (UTC+5:30) &nbsp;|&nbsp;
  Broker: {broker} &nbsp;|&nbsp; Updated: {upd} &nbsp;|&nbsp; Now: {now}
</div>
<script>var lb=document.getElementById('lb');if(lb)lb.scrollTop=lb.scrollHeight;</script>
</body>
</html>""".format(
        css=CSS,
        b1=kpi("Balance",    "${}".format(balance), "#60a5fa"),
        b2=kpi("Equity",     "${}".format(equity),  "#60a5fa"),
        b3=kpi("Today Trades", str(ts["n"])),
        b4=kpi("Today WR",
               "{:.1f}%".format(ts["wr"]) if ts["n"] > 0 else "—",
               wr_clr(ts["wr"]) if ts["n"] > 0 else "#64748b"),
        b5=kpi("Today P&amp;L",
               pnl_str(ts["net"]) if ts["n"] > 0 else "—",
               pnl_clr(ts["net"])),
        b6=kpi("All Trades", str(all_s["n"])),
        b7=kpi("All WR",
               "{:.1f}%".format(all_s["wr"]) if all_s["n"] > 0 else "—",
               wr_clr(all_s["wr"]) if all_s["n"] > 0 else "#64748b"),
        b8=kpi("All PF",
               "{:.2f}".format(all_s["pf"]) if all_s["n"] > 0 else "—",
               pf_clr(all_s["pf"]) if all_s["n"] > 0 else "#64748b"),
        b9=kpi("All Net P&amp;L",
               pnl_str(all_s["net"]) if all_s["n"] > 0 else "—",
               pnl_clr(all_s["net"])),
        b10=kpi("Updated", upd_ist, "#64748b"),
        gold_p=gold_panel, btc_p=btc_panel,
        log=l_html, s_tbl=s_tbl, e_tbl=e_tbl, rows=rows,
        broker=broker, upd=upd_ist, now=now_ist,
    )


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/dashboard"):
            body = generate_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            body = json.dumps(load_status(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/trades":
            data = {"GOLD": load_csv(GOLD_CSV), "BTC": load_csv(BTC_CSV)}
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    print("Strategy H XM Dashboard  ->  http://localhost:{}".format(PORT))
    print("GOLD  : {}".format(GOLD_CSV))
    print("BTC   : {}".format(BTC_CSV))
    print("Status: {}".format(XM_STATUS))
    srv = HTTPServer(("localhost", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        srv.server_close()


if __name__ == "__main__":
    main()
