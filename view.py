#!/usr/bin/env python3
"""Live terminal view. Reads logs/state.json written every second by bot.py.  Ctrl-C to exit."""
import json, os, time
from rich.live import Live
from rich.table import Table
from rich.console import Group
from rich.panel import Panel

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "state.json")

def f(x, n=4):
    return "-" if x is None else (f"{x:.{n}f}" if isinstance(x, float) else str(x))

def render():
    try: s = json.load(open(P))
    except Exception as e: return Panel(f"waiting for {P}: {e}")
    e = s.get("eval", {})
    head = (f"[b]{e.get('slug','-')}[/b]  left {f(e.get('secs_left'),0)}s   spot {f(e.get('spot'),2)}  "
            f"TWAP60 {f(e.get('twap'),2)}  to-beat {f(e.get('K'),2)}\n"
            f"sigma {f(e.get('sigma_ann'),3)} ann ({s.get('sigma_src')})  model {e.get('method','-')}  "
            f"feed lag {f(e.get('feed_lag'),1)}s  blocked {e.get('blocked') or e.get('status') or '-'}\n"
            f"mode {s.get('mode','paper')}  reason {s.get('live_reason','')}  kill {s.get('kill')}  "
            f"PnL ${s.get('realized_pnl',0):.2f}  trades {s.get('n_trades')}  wins {s.get('n_wins')}  "
            f"updated {s.get('updated_la','')[11:19]} PT  pid {s.get('pid')}")
    t = Table(title="Sides", expand=True)
    for c in ("side", "fair", "bid", "ask", "ask size", "fee/sh", "edge", "book age"): t.add_column(c)
    for side, d in (e.get("sides") or {}).items():
        edge = d.get("edge")
        col = "green" if edge is not None and edge >= 0.05 else ""
        t.add_row(side, f(d.get("fair"),3), f(d.get("bid"),2), f(d.get("ask"),2), f(d.get("ask_size"),1),
                  f(d.get("fee_ps"),4), f"[{col}]{f(edge,3)}[/{col}]" if col else f(edge,3), f(d.get("book_age"),1))
    mode = s.get("mode") or "paper"
    title = "polybtc — PAPER" if mode == "paper" else f"polybtc — {mode.upper()}"
    o = Table(title="Open positions (official settlement still pending)", expand=True)
    for c in ("slug", "side", "shares", "vwap", "fee", "fair", "edge", "entry PT"): o.add_column(c)
    for p in s.get("open_positions", []):
        entered = p.get("entry_time_la") or ""
        o.add_row(str(p.get("slug") or "")[-10:], str(p.get("side") or ""), f(p.get("shares"),2), f(p.get("ask_vwap"),3),
                  f(p.get("fee_usd"),3), f(p.get("fair_prob"),3), f(p.get("edge"),3), entered[11:19])
    return Group(Panel(head, title=title), t, o)

with Live(render(), refresh_per_second=2, screen=False) as live:
    while True:
        time.sleep(0.5); live.update(render())
