#!/usr/bin/env python3
"""Risk: morning review of the paper log.

  python risk_report.py --day YYYY-MM-DD     (default: yesterday, America/Los_Angeles)

Green week = the 7 consecutive calendar days ending on --day, each with
net paper PnL > 0 (a day with zero trades is NOT green). Prints PASS/FAIL.
Also writes reports/<day>.md.
"""
import argparse, csv, json, os, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "logs")
TZ = ZoneInfo("America/Los_Angeles")


def day_of(ts_la):
    return ts_la[:10] if ts_la else None


OFFICIAL = {"gamma_official", "gamma_outcome_prices", "gamma_final_price"}


def load_trades():
    """Official settlements only. UNKNOWN and feed-only rows are not results."""
    p = os.path.join(LOG, "trades.csv")
    if not os.path.exists(p): return [], 0
    by_key = {}
    unresolved = 0
    for r in csv.DictReader(open(p)):
        for k in ("ask_vwap", "fee_usd", "fee_per_share", "fair_prob", "edge", "shares", "cost_usd", "pnl_usd"):
            try: r[k] = float(r[k])
            except Exception: r[k] = None
        key = (r.get("slug"), r.get("side"), r.get("entry_time_la"))
        official = r.get("result") in ("WIN", "LOSS") and r.get("settle_source") in OFFICIAL
        if not official:
            unresolved += 1
            by_key.setdefault(key, None)
            continue
        by_key[key] = r
    rows = [row for row in by_key.values() if row]
    rows.sort(key=lambda row: row.get("entry_time_la") or "")
    return rows, unresolved


def jsonl(name):
    p = os.path.join(LOG, name)
    if not os.path.exists(p): return
    for line in open(p):
        try: yield json.loads(line)
        except Exception: continue


def max_dd(pnls):
    peak = cum = dd = 0.0
    for x in pnls:
        cum += x; peak = max(peak, cum); dd = max(dd, peak - cum)
    return dd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=(datetime.now(TZ).date() - timedelta(days=1)).isoformat())
    a = ap.parse_args()
    D = a.day
    L = []
    pr = lambda s="": L.append(s)
    trades, unresolved = load_trades()
    by_day = defaultdict(float)
    for t in trades:
        if t["pnl_usd"] is not None: by_day[day_of(t["entry_time_la"])] += t["pnl_usd"]
    td = sorted([t for t in trades if day_of(t["entry_time_la"]) == D], key=lambda t: t["entry_time_la"])
    pr(f"# polybtc risk report for {D} (America/Los_Angeles)")
    pr()
    pr("Historical paper rows are incomplete. Rows without an official Gamma settlement")
    pr("(UNKNOWN, or a feed-only TWAP result) are excluded and are not losses.")
    pr(f"Excluded unresolved rows in the whole file: {unresolved}.")
    pr("A green week does not enable live trading and is not evidence of a profitable strategy.")
    pr()
    n = len(td); wins = sum(t["result"] == "WIN" for t in td)
    net = sum(t["pnl_usd"] or 0 for t in td); fees = sum(t["fee_usd"] or 0 for t in td)
    cost = sum(t["cost_usd"] or 0 for t in td)
    pr(f"Trades: {n}   wins: {wins}   win rate: {wins/n:.1%}" if n else "Trades: 0")
    if n:
        avg_fair = sum(t["fair_prob"] for t in td) / n
        avg_edge = sum(t["edge"] for t in td) / n
        # realized edge per share = (1 if win) - (price + fee per share), averaged per share-weighted
        sh = sum(t["shares"] for t in td)
        realized_edge = sum((t["pnl_usd"]) for t in td) / sh if sh else 0
        pr(f"Net PnL: ${net:.2f}  (fees ${fees:.2f}, staked ${cost:.2f}, ROI {net/cost:.1%})")
        pr(f"Avg predicted fair prob {avg_fair:.3f} vs realized win rate {wins/n:.3f}")
        pr(f"Avg edge at entry {avg_edge:+.3f}/share vs realized {realized_edge:+.3f}/share")
        pr(f"Max drawdown (sequential paper PnL): ${max_dd([t['pnl_usd'] for t in td]):.2f}")
        bys = defaultdict(list)
        for t in td: bys[t["side"]].append(t)
        for s, ts in bys.items():
            pr(f"  {s}: {len(ts)} trades, {sum(x['result']=='WIN' for x in ts)} wins, ${sum(x['pnl_usd'] for x in ts):.2f}")
    # calibration: model P(Up) from ticks vs window outcomes
    outcome = {}
    for e in jsonl("events.jsonl"):
        if e.get("type") == "window_final" and e.get("outcome"): outcome[e["slug"]] = e["outcome"]
    buckets = defaultdict(lambda: [0, 0, 0.0, 0.0])   # n, ups, sum model p, sum market mid
    for tk in jsonl("ticks.jsonl"):
        if day_of(tk.get("ts_la")) != D or tk.get("slug") not in outcome or tk.get("p_up") is None: continue
        if (tk.get("secs_left") or 0) < 5: continue
        p = tk["p_up"]; b = min(9, int(p * 10))
        up = tk.get("sides", {}).get("Up", {}); mkt = up.get("ask")
        bk = buckets[b]; bk[0] += 1; bk[1] += outcome[tk["slug"]] == "Up"; bk[2] += p; bk[3] += mkt or 0
    pr(); pr("Calibration of Scout P(Up), 5s ticks vs final outcome (ticks are autocorrelated within a window):")
    pr("  bucket     n   model  actual  Up-ask")
    for b in sorted(buckets):
        n_, u, sp, sm = buckets[b]
        pr(f"  {b/10:.1f}-{(b+1)/10:.1f} {n_:5d}  {sp/n_:.3f}  {u/n_:.3f}   {sm/n_:.3f}")
    if n:
        tb = defaultdict(lambda: [0, 0, 0.0])
        for t in td:
            b = min(9, int(t["fair_prob"] * 10)); tb[b][0] += 1; tb[b][1] += t["result"] == "WIN"; tb[b][2] += t["fair_prob"]
        pr("Trade calibration (fair prob of side bought):")
        for b in sorted(tb): pr(f"  {b/10:.1f}-{(b+1)/10:.1f} n={tb[b][0]} model={tb[b][2]/tb[b][0]:.3f} won={tb[b][1]/tb[b][0]:.3f}")
    # anomalies
    an = defaultdict(int); ex = {}
    windows = 0
    for e in jsonl("events.jsonl"):
        if day_of(e.get("ts_la")) != D: continue
        if e.get("type") == "window_final": windows += 1
        if e.get("type") == "anomaly" or e.get("type", "").endswith("_error"):
            k = e.get("kind") or e["type"]; an[k] += 1; ex.setdefault(k, e)
    stale = sum(1 for tk in jsonl("ticks.jsonl") if day_of(tk.get("ts_la")) == D and
                any((s or {}).get("book_age", 0) > 30 for s in (tk.get("sides") or {}).values()))
    if stale: an["stale_book_ticks(>30s)"] = stale
    pr(); pr(f"Windows settled/logged: {windows} of 288")
    pr("Anomalies: " + (", ".join(f"{k}={v}" for k, v in sorted(an.items())) if an else "none"))
    for k, e in ex.items(): pr(f"  e.g. {k}: {json.dumps(e)[:220]}")
    # green week
    d0 = date.fromisoformat(D)
    days = [(d0 - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    pr(); pr("Green week check (7 consecutive days, each net PnL > 0):")
    ok = True
    for d in days:
        v = by_day.get(d); g = v is not None and v > 0; ok &= g
        pr(f"  {d}: {'no trades' if v is None else f'${v:.2f}'}  {'green' if g else 'NOT green'}")
    pr(f"GREEN WEEK: {'PASS' if ok else 'FAIL'}")
    pr("PASS/FAIL is a log check only. It does not place orders or turn live mode on.")
    out = "\n".join(L)
    print(out)
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    open(os.path.join(HERE, "reports", f"{D}.md"), "w").write(out + "\n")


if __name__ == "__main__":
    main()
