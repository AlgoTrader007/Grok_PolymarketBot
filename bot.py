#!/usr/bin/env python3
"""polybtc — PAPER-ONLY Scout/Trader/Settler for Polymarket 5-minute BTC Up/Down.

Single asyncio process. Reads only PUBLIC data:
  * Chainlink BTC/USD spot + TWAP60 via Polymarket RTDS (wss://ws-live-data.polymarket.com)
  * Market discovery via Gamma API (gamma-api.polymarket.com/events?slug=btc-updown-5m-<start>)
  * Order books via CLOB market websocket + REST /book
There is NO order-placing code, no wallet, no keys. Paper fills only.
"""
import asyncio, csv, json, math, os, signal, sys, time, tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp, websockets
import model

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = tomllib.load(open(os.path.join(HERE, "config.toml"), "rb"))
TC, SC, AC = CFG["trader"], CFG["scout"], CFG["app"]
TZ = ZoneInfo(AC["timezone"])
LOG = os.path.join(HERE, AC["log_dir"])
os.makedirs(LOG, exist_ok=True)

GAMMA = "https://gamma-api.polymarket.com/events?slug="
CLOB_BOOK = "https://clob.polymarket.com/book?token_id="
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"
RTDS_SUB = {"action": "subscribe", "subscriptions": [
    {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})},
    {"topic": "crypto_prices_twap_sixty", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})}]}
WIN = 300
CSV_FIELDS = ["entry_time_la", "settle_time_la", "slug", "side", "ask_vwap", "best_ask", "fee_usd", "fee_per_share",
              "fair_prob", "edge", "shares", "cost_usd", "price_to_beat", "final_price", "result", "pnl_usd",
              "settle_source", "secs_left_at_entry", "sigma_ann", "partial"]


def la(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), TZ).isoformat(timespec="milliseconds")


def jlog(name, rec):
    rec = {"ts_la": la(), "ts": round(time.time(), 3), **rec}
    with open(os.path.join(LOG, name), "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


class State:
    def __init__(self):
        self.spot = {}          # unix sec -> Chainlink spot
        self.twap = {}          # unix sec -> Chainlink TWAP60
        self.last_feed_wall = 0.0
        self.markets = {}       # start -> market dict
        self.books = {}         # token -> {"bids":{p:s}, "asks":{p:s}, "t":wall}
        self.positions = []     # open paper trades
        self.entries = {}       # (start, side) -> count
        self.realized = 0.0
        self.n_trades = 0
        self.n_wins = 0
        self.sigma = None
        self.sigma_src = "none"
        self.last_eval = {}
        self.boot_sigma = None
        self.started = time.time()
        self.last_gap_logged = 0
        self.cb = {}            # unix sec -> last Coinbase BTC-USD trade (lag nowcast only)
        self.cb_last = None     # (wall_ts, price)


S = State()


# ---------------------------------------------------------------- Scout: price feed
def ingest(topic, pts):
    d = S.spot if topic == "crypto_prices" or topic == "crypto_prices_chainlink" else S.twap
    for p in pts:
        try:
            d[int(p["timestamp"]) // 1000] = float(p["value"])
        except Exception:
            pass


async def rtds_loop():
    while True:
        try:
            async with websockets.connect(RTDS_WS, open_timeout=10, ping_interval=20, max_size=2**22) as ws:
                jlog("events.jsonl", {"type": "rtds_connected"})
                async def poller():
                    while True:
                        await ws.send(json.dumps(RTDS_SUB))
                        await asyncio.sleep(SC["poll_interval_s"])
                pt = asyncio.create_task(poller())
                try:
                    while True:
                        m = await asyncio.wait_for(ws.recv(), 15)
                        if not m or m[0] != "{":
                            continue
                        d = json.loads(m)
                        topic, p = d.get("topic"), d.get("payload") or {}
                        if topic not in ("crypto_prices", "crypto_prices_chainlink", "crypto_prices_twap_sixty"):
                            continue
                        if "data" in p:
                            ingest(topic, p["data"])
                        elif "value" in p:
                            ingest(topic, [p])
                        S.last_feed_wall = time.time()
                finally:
                    pt.cancel()
        except Exception as e:
            jlog("events.jsonl", {"type": "rtds_error", "err": repr(e)[:200]})
            await asyncio.sleep(2)


async def coinbase_loop():
    """Public Coinbase ticker. Used ONLY to nowcast the 1-4 s by which the Chainlink
    snapshot lags real time: S_now = CL_spot(ts) * CB(now)/CB(ts). Settlement still uses Chainlink."""
    while True:
        try:
            async with websockets.connect("wss://ws-feed.exchange.coinbase.com", open_timeout=10, ping_interval=20) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
                while True:
                    d = json.loads(await asyncio.wait_for(ws.recv(), 30))
                    if d.get("type") == "ticker" and d.get("price"):
                        p, now = float(d["price"]), time.time()
                        S.cb[int(now)] = p; S.cb_last = (now, p)
        except Exception as e:
            jlog("events.jsonl", {"type": "coinbase_error", "err": repr(e)[:200]})
            await asyncio.sleep(3)


def nowcast(ts, spot):
    """Return (spot_now, t_eff, used_nowcast)."""
    if not SC.get("nowcast", True) or not S.cb_last or time.time() - S.cb_last[0] > 5:
        return spot, ts, False
    ref = None
    for k in range(0, 4):
        ref = S.cb.get(ts - k) or ref
        if ref: break
    if not ref:
        return spot, ts, False
    r = S.cb_last[1] / ref
    if abs(r - 1) > 0.01:      # implausible; ignore
        return spot, ts, False
    return spot * r, time.time(), True


def prune():
    cut = int(time.time()) - 3 * 3600
    for d in (S.spot, S.twap, S.cb):
        for k in [k for k in d if k < cut]:
            del d[k]


def latest(d):
    if not d:
        return None, None
    k = max(d)
    return k, d[k]


async def bootstrap_sigma(session):
    """Seed vol from Coinbase 1-min candles (public) until enough Chainlink history accrues."""
    try:
        async with session.get("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60",
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            c = await r.json()
        closes = [x[4] for x in sorted(c)[-120:]]
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        S.boot_sigma = math.sqrt(sum(r * r for r in rets) / len(rets) / 60)
        jlog("events.jsonl", {"type": "sigma_bootstrap", "sigma_ann": model.sec_to_ann(S.boot_sigma), "n": len(rets)})
    except Exception as e:
        jlog("events.jsonl", {"type": "sigma_bootstrap_failed", "err": repr(e)[:200]})


def update_sigma():
    now = max(S.spot) if S.spot else int(time.time())
    step = SC["vol_return_step_s"]
    slow, ns = model.realized_sigma(S.spot, now, SC["vol_slow_window_s"], step)
    fast, nf = model.realized_sigma(S.spot, now, SC["vol_fast_window_s"], step)
    full_n = SC["vol_slow_window_s"] // step
    if slow is None and S.boot_sigma is None:
        S.sigma, S.sigma_src = None, "none"; return
    if slow is None:
        sig, src = S.boot_sigma, "coinbase_bootstrap"
    else:
        w = min(1.0, ns / full_n)
        base = slow if S.boot_sigma is None else math.sqrt(w * slow**2 + (1 - w) * S.boot_sigma**2)
        sig = math.sqrt(0.5 * base**2 + 0.5 * fast**2) if fast else base
        src = f"chainlink_rv(n={ns},w={w:.2f})"
    lo, hi = model.ann_to_sec(SC["vol_floor_ann"]), model.ann_to_sec(SC["vol_cap_ann"])
    S.sigma = min(hi, max(lo, sig)) * SC["vol_mult"]
    S.sigma_src = src


# ---------------------------------------------------------------- market discovery
async def fetch_event(session, start):
    async with session.get(GAMMA + f"btc-updown-5m-{start}", timeout=aiohttp.ClientTimeout(total=10)) as r:
        evs = await r.json()
    if not evs:
        return None
    e = evs[0]
    m = e["markets"][0]
    outs = json.loads(m["outcomes"]); toks = json.loads(m["clobTokenIds"])
    fs = m.get("feeSchedule") or {}
    return {"start": start, "end": start + WIN, "slug": e["slug"], "title": e.get("title"),
            "up": toks[outs.index("Up")], "down": toks[outs.index("Down")],
            "fee_rate": float(fs.get("rate", TC["fee_rate_default"])) if m.get("feesEnabled", True) else 0.0,
            "fee_exp": float(fs.get("exponent", 1)), "meta": e.get("eventMetadata") or {},
            "closed": m.get("closed"), "outcomePrices": m.get("outcomePrices"),
            "accepting": m.get("acceptingOrders"), "tick": m.get("orderPriceMinTickSize")}


async def discovery_loop(session):
    while True:
        now = int(time.time()); cur = now - now % WIN
        for st in (cur, cur + WIN):
            mk = S.markets.get(st)
            if mk is None:
                try:
                    ev = await fetch_event(session, st)
                    if ev:
                        if mk is None:
                            jlog("events.jsonl", {"type": "market_discovered", "slug": ev["slug"], "fee_rate": ev["fee_rate"]})
                            ev["ptb"] = None; ev["ptb_src"] = None
                            S.markets[st] = ev
                        else:
                            mk["meta"] = ev["meta"]
                except Exception as e:
                    jlog("events.jsonl", {"type": "gamma_error", "start": st, "err": repr(e)[:200]})
        for st in [k for k in S.markets if k < cur - 3600]:
            del S.markets[st]
        await asyncio.sleep(5)


def price_to_beat(mk):
    if mk["ptb"] is not None:
        return mk["ptb"]
    st = mk["start"]
    if st in S.twap:
        mk["ptb"], mk["ptb_src"] = S.twap[st], "rtds_twap60@start"
    elif mk["meta"].get("priceToBeat"):
        mk["ptb"], mk["ptb_src"] = float(mk["meta"]["priceToBeat"]), "gamma_priceToBeat"
    if mk["ptb"] is not None:
        jlog("events.jsonl", {"type": "price_to_beat", "slug": mk["slug"], "ptb": mk["ptb"], "src": mk["ptb_src"]})
    return mk["ptb"]


# ---------------------------------------------------------------- books
def apply_book(tok, bids, asks):
    S.books[tok] = {"bids": {float(x["price"]): float(x["size"]) for x in bids},
                    "asks": {float(x["price"]): float(x["size"]) for x in asks}, "t": time.time()}


async def book_ws_loop():
    while True:
        now = int(time.time()); cur = now - now % WIN
        toks = [t for st in (cur, cur + WIN) if st in S.markets for t in (S.markets[st]["up"], S.markets[st]["down"])]
        if not toks:
            await asyncio.sleep(1); continue
        try:
            async with websockets.connect(CLOB_WS, open_timeout=10, ping_interval=None, max_size=2**23) as ws:
                await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                async def hb():
                    while True:
                        await asyncio.sleep(10); await ws.send("PING")
                h = asyncio.create_task(hb())
                deadline = cur + WIN + 1   # resubscribe after each rollover
                try:
                    while time.time() < deadline:
                        try:
                            m = await asyncio.wait_for(ws.recv(), max(0.5, deadline - time.time()))
                        except asyncio.TimeoutError:
                            break
                        if not m or m in ("PONG", "pong") or m[0] not in "[{":
                            continue
                        d = json.loads(m)
                        for ev in (d if isinstance(d, list) else [d]):
                            et = ev.get("event_type")
                            if et == "book":
                                apply_book(ev["asset_id"], ev.get("bids", []), ev.get("asks", []))
                            elif et == "price_change":
                                for pc in ev.get("price_changes", []):
                                    b = S.books.get(pc["asset_id"])
                                    if not b: continue
                                    side = b["bids"] if pc["side"] == "BUY" else b["asks"]
                                    p, s = float(pc["price"]), float(pc["size"])
                                    if s == 0: side.pop(p, None)
                                    else: side[p] = s
                                    b["t"] = time.time()
                        trader_eval()
                finally:
                    h.cancel()
        except Exception as e:
            jlog("events.jsonl", {"type": "book_ws_error", "err": repr(e)[:200]})
            await asyncio.sleep(2)


async def book_rest_loop(session):
    """Resync books by REST every 15s (and covers ws outages)."""
    while True:
        now = int(time.time()); cur = now - now % WIN
        mk = S.markets.get(cur)
        if mk:
            for tok in (mk["up"], mk["down"]):
                b = S.books.get(tok)
                if b and time.time() - b["t"] < 15:
                    continue
                try:
                    async with session.get(CLOB_BOOK + tok, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        j = await r.json()
                    if "asks" in j:
                        apply_book(tok, j.get("bids", []), j.get("asks", []))
                except Exception as e:
                    jlog("events.jsonl", {"type": "book_rest_error", "err": repr(e)[:200]})
        await asyncio.sleep(3)


# ---------------------------------------------------------------- Trader (paper)
def compute_fair(mk):
    K = price_to_beat(mk)
    ts, spot = latest(S.spot)
    tt, tw = latest(S.twap)
    if K is None or ts is None or S.sigma is None:
        return None
    s_now, t_eff, nc = nowcast(ts, spot)
    tau = mk["end"] - t_eff
    twap_now = tw if (tt is not None and abs(tt - ts) <= 2) else None
    # the TWAP-anchor branch works on whole seconds of observed Chainlink data; nowcast feeds S only
    if twap_now is not None and mk["end"] - ts < 60:
        # anchor on official TWAP60 at data second ts; future spot path starts from nowcast S
        p, meth = model.fair_p_up(K, s_now, mk["end"] - ts, S.sigma, twap_now, S.spot, ts, SC["prob_clip"])
    else:
        p, meth = model.fair_p_up(K, s_now, tau, S.sigma, None, None, None, SC["prob_clip"])
    return {"K": K, "spot": spot, "spot_now": s_now, "nowcast": nc, "cb": S.cb_last[1] if S.cb_last else None,
            "twap": tw, "data_ts": ts, "tau": round(tau, 2), "p_up": p, "method": meth + ("+nowcast" if nc else "")}


_last_eval_t = 0.0
def trader_eval(force=False):
    global _last_eval_t
    now = time.time()
    if not force and now - _last_eval_t < 0.25:
        return
    _last_eval_t = now
    cur = int(now) - int(now) % WIN
    mk = S.markets.get(cur)
    if not mk:
        return
    f = compute_fair(mk)
    if not f:
        S.last_eval = {"slug": mk["slug"], "status": "waiting (price-to-beat/sigma/feed)"}; return
    feed_lag = now - f["data_ts"]
    secs_left, elapsed = mk["end"] - now, now - mk["start"]
    ev = {"slug": mk["slug"], "start": mk["start"], "end": mk["end"], "secs_left": round(secs_left, 1), **f,
          "feed_lag": round(feed_lag, 2), "sigma_ann": model.sec_to_ann(S.sigma), "sides": {}}
    blocked = []
    if feed_lag > TC["max_feed_lag_s"]: blocked.append("feed_stale")
    if secs_left < TC["min_secs_left"]: blocked.append("too_late")
    if elapsed < TC["min_secs_elapsed"]: blocked.append("too_early")
    for side, tok, fair in (("Up", mk["up"], f["p_up"]), ("Down", mk["down"], 1 - f["p_up"])):
        b = S.books.get(tok)
        sd = {"fair": fair, "ask": None, "edge": None}
        if not b or not b["asks"]:
            sd["status"] = "no_book"; ev["sides"][side] = sd; continue
        asks = sorted(b["asks"].items())
        best = asks[0][0]
        fps = model.fee_per_share(best, mk["fee_rate"], mk["fee_exp"])
        sd.update(ask=best, ask_size=asks[0][1], fee_ps=fps, edge=fair - (best + fps), book_age=round(now - b["t"], 1),
                  bid=max(b["bids"]) if b["bids"] else None)
        ev["sides"][side] = sd
        sblock = list(blocked)
        if now - b["t"] > TC["max_book_age_s"]: sblock.append("book_stale")
        if sd["edge"] < TC["edge_threshold"] or best > TC["max_price"]:
            continue
        if S.entries.get((cur, side), 0) >= TC["entries_per_side"]: sblock.append("already_entered")
        # walk the book: only levels that still satisfy edge & max price
        shares = cost = fee = 0.0
        for p, sz in asks:
            fp = model.fee_per_share(p, mk["fee_rate"], mk["fee_exp"])
            if p > TC["max_price"] or fair - (p + fp) < TC["edge_threshold"]:
                break
            take = min(sz, (TC["size_usd"] - cost) / p)
            shares += take; cost += take * p; fee += take * fp
            if cost >= TC["size_usd"] - 1e-9:
                break
        fee = round(fee, 5)
        sig = {"type": "signal", "slug": mk["slug"], "side": side, "best_ask": best, "fee_per_share": round(fps, 5),
               "fair_prob": round(fair, 4), "edge": round(sd["edge"], 4), "fillable_shares": round(shares, 2),
               "fillable_usd": round(cost, 2), "secs_left": round(secs_left, 1), "feed_lag": round(feed_lag, 2),
               "spot": f["spot"], "twap": f["twap"], "price_to_beat": f["K"], "method": f["method"],
               "sigma_ann": round(model.sec_to_ann(S.sigma), 3), "blocked": sblock}
        if shares < TC["min_shares"]: sblock.append("insufficient_depth")
        key = (cur, side, tuple(sblock))
        if sblock:
            if S.last_eval.get("_sigkey") != key:   # de-dupe repeated blocked signals
                jlog("signals.jsonl", sig); S.last_eval["_sigkey"] = key
            continue
        jlog("signals.jsonl", sig)
        vwap = cost / shares
        pos = {"slug": mk["slug"], "start": cur, "end": mk["end"], "side": side, "shares": round(shares, 4),
               "ask_vwap": round(vwap, 4), "best_ask": best, "cost_usd": round(cost, 4), "fee_usd": fee,
               "fee_per_share": round(fee / shares, 5), "fair_prob": round(fair, 4),
               "edge": round(fair - vwap - fee / shares, 4), "price_to_beat": f["K"], "entry_ts": now,
               "entry_time_la": la(now), "secs_left_at_entry": round(secs_left, 1),
               "sigma_ann": round(model.sec_to_ann(S.sigma), 3), "partial": cost < TC["size_usd"] - 0.01}
        S.positions.append(pos)
        S.entries[(cur, side)] = S.entries.get((cur, side), 0) + 1
        # consume the paper-filled liquidity locally so we don't double count it
        for p, sz in asks:
            if shares <= 0: break
            t = min(sz, shares); b["asks"][p] = sz - t; shares -= t
            if b["asks"][p] <= 1e-9: b["asks"].pop(p)
        jlog("trades.jsonl", {"type": "paper_entry", **pos})
    ev["blocked"] = blocked
    S.last_eval = {**ev, "_sigkey": S.last_eval.get("_sigkey")}


# ---------------------------------------------------------------- settlement
def write_csv(row):
    path = os.path.join(LOG, "trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new: w.writeheader()
        w.writerow({k: row.get(k) for k in CSV_FIELDS})


async def settle_loop(session):
    pending = {}   # start -> {"feed_final":..., "t":...}
    while True:
        now = time.time()
        # 1) feed settlement as soon as TWAP60 at T_end arrives
        for st, mk in list(S.markets.items()):
            if mk["end"] > now or mk.get("feed_final") is not None:
                continue
            if mk["end"] in S.twap:
                mk["feed_final"] = S.twap[mk["end"]]
                K = mk["ptb"]
                outcome = None if K is None else ("Up" if mk["feed_final"] >= K else "Down")
                mk["feed_outcome"] = outcome
                jlog("events.jsonl", {"type": "window_feed_settled", "slug": mk["slug"], "ptb": K,
                                      "final": mk["feed_final"], "outcome": outcome})
                pending[st] = now
            elif now - mk["end"] > 30 and not mk.get("gap_logged"):
                mk["gap_logged"] = True
                jlog("events.jsonl", {"type": "anomaly", "kind": "no_twap_at_end", "slug": mk["slug"]})
                pending[st] = now
        # 2) official result from Gamma (outcomePrices / eventMetadata.finalPrice)
        for st in list(pending):
            mk = S.markets.get(st)
            if not mk: pending.pop(st); continue
            official = None
            if now - mk["end"] >= 20 and now - mk.get("_last_gamma", 0) >= 15:
                mk["_last_gamma"] = now
                try:
                    ev = await fetch_event(session, st)
                    meta = ev["meta"] if ev else {}
                    op = json.loads(ev["outcomePrices"]) if ev and ev.get("outcomePrices") else None
                    if ev and ev.get("closed") and op and max(map(float, op)) > 0.99:
                        official = "Up" if float(op[0]) > 0.5 else "Down"
                    elif meta.get("finalPrice") and meta.get("priceToBeat"):
                        official = "Up" if float(meta["finalPrice"]) >= float(meta["priceToBeat"]) else "Down"
                    if official:
                        mk["official_final"] = meta.get("finalPrice"); mk["official_ptb"] = meta.get("priceToBeat")
                except Exception as e:
                    jlog("events.jsonl", {"type": "gamma_error", "start": st, "err": repr(e)[:200]})
            timeout = now - mk["end"] > 900
            if official or timeout:
                res = official or mk.get("feed_outcome")
                src = "gamma_official" if official else "rtds_twap60_feed"
                if official and mk.get("feed_outcome") and official != mk["feed_outcome"]:
                    jlog("events.jsonl", {"type": "anomaly", "kind": "settle_mismatch", "slug": mk["slug"],
                                          "feed": mk["feed_outcome"], "official": official})
                if mk.get("official_ptb") and mk.get("ptb") and abs(float(mk["official_ptb"]) - mk["ptb"]) > 0.01:
                    jlog("events.jsonl", {"type": "anomaly", "kind": "ptb_mismatch", "slug": mk["slug"],
                                          "ours": mk["ptb"], "official": mk["official_ptb"]})
                jlog("events.jsonl", {"type": "window_final", "slug": mk["slug"], "outcome": res, "src": src,
                                      "feed_final": mk.get("feed_final"), "official_final": mk.get("official_final"),
                                      "ptb": mk.get("ptb"), "official_ptb": mk.get("official_ptb")})
                for pos in [p for p in S.positions if p["start"] == st]:
                    win = res == pos["side"]
                    pnl = (pos["shares"] if win else 0.0) - pos["cost_usd"] - pos["fee_usd"]
                    row = {**pos, "settle_time_la": la(), "final_price": mk.get("official_final") or mk.get("feed_final"),
                           "result": ("WIN" if win else "LOSS") if res else "UNKNOWN", "pnl_usd": round(pnl, 4),
                           "settle_source": src}
                    jlog("trades.jsonl", {"type": "paper_settle", **row})
                    write_csv(row)
                    S.positions.remove(pos)
                    S.realized += pnl; S.n_trades += 1; S.n_wins += int(win)
                pending.pop(st)
        await asyncio.sleep(1)


# ---------------------------------------------------------------- housekeeping / status
async def housekeeping():
    last_tick = 0
    while True:
        now = time.time()
        update_sigma(); prune(); trader_eval(force=True)
        if S.last_feed_wall and now - S.last_feed_wall > 10 and now - S.last_gap_logged > 60:
            S.last_gap_logged = now
            jlog("events.jsonl", {"type": "anomaly", "kind": "feed_gap", "secs": round(now - S.last_feed_wall, 1)})
        e = S.last_eval
        if now - last_tick >= AC["tick_log_every_s"] and e.get("sides"):
            last_tick = now
            jlog("ticks.jsonl", {k: e.get(k) for k in ("slug", "secs_left", "K", "spot", "spot_now", "cb", "twap", "tau", "p_up",
                                                       "method", "feed_lag", "sigma_ann", "sides", "blocked")})
        st = {"updated_la": la(), "uptime_s": int(now - S.started), "eval": {k: v for k, v in e.items() if k != "_sigkey"},
              "sigma_src": S.sigma_src, "open_positions": S.positions, "realized_pnl": round(S.realized, 4),
              "n_trades": S.n_trades, "n_wins": S.n_wins, "feed_age_s": round(now - S.last_feed_wall, 1),
              "markets": sorted(m["slug"] for m in S.markets.values()), "pid": os.getpid()}
        tmp = os.path.join(LOG, "state.json.tmp")
        with open(tmp, "w") as f: json.dump(st, f, default=str)
        os.replace(tmp, os.path.join(LOG, "state.json"))
        await asyncio.sleep(1)


def restore_pnl():
    """Rebuild running totals (and today's entries) from trades.csv after a restart."""
    p = os.path.join(LOG, "trades.csv")
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            try:
                S.realized += float(r["pnl_usd"]); S.n_trades += 1; S.n_wins += r["result"] == "WIN"
            except Exception: pass
    # re-load unsettled entries
    p = os.path.join(LOG, "trades.jsonl")
    if os.path.exists(p):
        ent, done = {}, set()
        for line in open(p):
            try: r = json.loads(line)
            except Exception: continue
            k = (r.get("slug"), r.get("side"), r.get("entry_ts"))
            if r.get("type") == "paper_entry": ent[k] = r
            elif r.get("type") == "paper_settle": done.add(k)
        for k, r in ent.items():
            if k not in done and time.time() - r["end"] < 900:
                r = {x: y for x, y in r.items() if x not in ("type", "ts", "ts_la")}
                S.positions.append(r); S.entries[(r["start"], r["side"])] = S.entries.get((r["start"], r["side"]), 0) + 1


async def main():
    restore_pnl()
    jlog("events.jsonl", {"type": "start", "pid": os.getpid(), "config": CFG, "restored_open": len(S.positions)})
    async with aiohttp.ClientSession(headers={"User-Agent": "polybtc-paper/1.0"}) as session:
        await bootstrap_sigma(session)
        # markets that restored positions belong to
        for pos in S.positions:
            if pos["start"] not in S.markets:
                try:
                    ev = await fetch_event(session, pos["start"])
                    if ev: ev["ptb"] = pos["price_to_beat"]; ev["ptb_src"] = "restored"; S.markets[pos["start"]] = ev
                except Exception: pass
        await asyncio.gather(rtds_loop(), coinbase_loop(), discovery_loop(session), book_ws_loop(), book_rest_loop(session),
                             settle_loop(session), housekeeping())


if __name__ == "__main__":
    for sgn in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sgn, lambda *_: sys.exit(0))
    asyncio.run(main())
