#!/usr/bin/env python3
"""polybtc — Scout/Trader/Settler for Polymarket 5-minute BTC Up/Down.

Paper mode is the default. Live orders are sent only after the operator sets
live.enabled and POLYBTC_LIVE_CONFIRM=YES on that machine. Public data:
  * Chainlink BTC/USD spot + TWAP60 via Polymarket RTDS (wss://ws-live-data.polymarket.com)
  * Market discovery via Gamma API (gamma-api.polymarket.com/events?slug=btc-updown-5m-<start>)
  * Order books via CLOB market websocket + REST /book
Live orders use the official polymarket-client against https://clob.polymarket.com.
There is no proxy, no geoblock bypass, and no order when eligibility checks fail.
"""
import argparse, asyncio, json, math, os, signal, sys, time, tomllib
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp, websockets
import gates
import live_exec
import model
import persist
import settlement

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
def la(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), TZ).isoformat(timespec="milliseconds")


def jlog(name, rec):
    rec = persist.redact_obj({"ts_la": la(), "ts": round(time.time(), 3), **rec}, S.secrets)
    with open(os.path.join(LOG, name), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec, default=str) + "\n")


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
        self.spot_recv = {}
        self.twap_recv = {}
        self.secrets = []
        self.book = None
        self.engine = None
        self.mode = "paper"
        self.live_reason = "live_disabled"
        self.shutdown = False
        self.pending_submits = []
        self.limits = {}


S = State()


# ---------------------------------------------------------------- Scout: price feed
def ingest(topic, pts):
    spot = topic in ("crypto_prices", "crypto_prices_chainlink")
    series = S.spot if spot else S.twap
    received = S.spot_recv if spot else S.twap_recv
    wall = time.time()
    for point in pts:
        try:
            sec = int(point["timestamp"]) // 1000
            if sec not in series:
                received[sec] = wall
            series[sec] = float(point["value"])
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
                        now_wall = time.time()
                        if S.last_feed_wall and now_wall - S.last_feed_wall > TC["max_feed_lag_s"]:
                            _taint_gap(S.last_feed_wall, now_wall)
                        if "data" in p:
                            ingest(topic, p["data"])
                        elif "value" in p:
                            ingest(topic, [p])
                        S.last_feed_wall = now_wall
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


def _taint_gap(start_wall, end_wall):
    first = int(start_wall) - int(start_wall) % WIN
    last = int(end_wall) - int(end_wall) % WIN
    for start in range(first, last + WIN, WIN):
        market = S.markets.get(start)
        if market is not None:
            market["feed_gap"] = True


def prune():
    cut = int(time.time()) - 3 * 3600
    for series in (S.spot, S.twap, S.cb, S.spot_recv, S.twap_recv):
        for key in [key for key in series if key < cut]:
            del series[key]


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
            "accepting": m.get("acceptingOrders"), "tick": m.get("orderPriceMinTickSize"),
            "min_order_size": m.get("orderMinSize"), "condition_id": m.get("conditionId"),
            "version": m.get("version") or e.get("version"),
            "resolution_source": m.get("resolutionSource") or e.get("resolutionSource"),
            "crypto_config": m.get("cryptoMarketConfig") or {},
            "neg_risk": m.get("negRisk")}


async def discovery_loop(session):
    while True:
        now = int(time.time()); cur = now - now % WIN
        for st in (cur, cur + WIN):
            mk = S.markets.get(st)
            try:
                ev = await fetch_event(session, st)
            except Exception as exc:
                jlog("events.jsonl", {"type": "gamma_error", "start": st, "err": repr(exc)[:200]})
                continue
            if not ev:
                continue
            if mk is None:
                ev["ptb"] = None
                ev["ptb_src"] = None
                ev["ptb_fresh"] = False
                ev["ptb_locked"] = False
                S.markets[st] = ev
                jlog("events.jsonl", {"type": "market_discovered", "slug": ev["slug"], "fee_rate": ev["fee_rate"],
                                      "tick": ev.get("tick"), "min_order_size": ev.get("min_order_size"),
                                      "version": ev.get("version")})
            else:
                for key in ("meta", "closed", "outcomePrices", "accepting", "tick", "min_order_size",
                            "fee_rate", "fee_exp", "resolution_source", "crypto_config", "version", "condition_id"):
                    if ev.get(key) is not None:
                        mk[key] = ev[key]
        open_starts = set()
        if S.book is not None:
            open_starts = {int(item["start"]) for item in S.book.positions if item.get("start") is not None}
            open_starts.update(int(item["start"]) for item in S.book.intents
                               if item.get("start") is not None and item.get("status") not in {"not_sent", "rejected", "absent", "unmatched"})
        for st in [key for key in S.markets if key < cur - 3600 and key not in open_starts]:
            del S.markets[st]
        await asyncio.sleep(5)


def price_to_beat(mk):
    if mk.get("ptb_locked") and mk.get("ptb") is not None:
        return mk["ptb"]
    st = mk["start"]
    if st in S.twap:
        fresh = gates.ptb_is_fresh(st, S.twap_recv.get(st), TC["max_feed_lag_s"])
        mk["ptb"] = S.twap[st]
        mk["ptb_src"] = "rtds_twap60@start" if fresh else "rtds_twap60_late"
        mk["ptb_fresh"] = fresh
        mk["ptb_locked"] = True
        jlog("events.jsonl", {"type": "price_to_beat", "slug": mk["slug"], "ptb": mk["ptb"], "src": mk["ptb_src"], "fresh": fresh})
    elif time.time() >= mk["end"] and (mk.get("meta") or {}).get("priceToBeat"):
        mk["ptb"] = float(mk["meta"]["priceToBeat"])
        mk["ptb_src"] = "gamma_priceToBeat"
        mk["ptb_fresh"] = False
        mk["ptb_locked"] = True
        jlog("events.jsonl", {"type": "price_to_beat", "slug": mk["slug"], "ptb": mk["ptb"], "src": mk["ptb_src"], "fresh": False})
    return mk.get("ptb")


# ---------------------------------------------------------------- books
def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_book(tok, bids, asks, meta=None):
    meta = meta or {}
    prev = S.books.get(tok) or {}
    exch = _num(meta.get("timestamp"))
    if exch and exch > 10**12:
        exch = exch / 1000.0
    tick = _num(meta.get("tick_size"))
    if tick is None:
        tick = prev.get("tick")
    minimum = _num(meta.get("min_order_size"))
    if minimum is None:
        minimum = prev.get("min_order_size")
    S.books[tok] = {
        "bids": {float(x["price"]): float(x["size"]) for x in bids},
        "asks": {float(x["price"]): float(x["size"]) for x in asks},
        "t": time.time(),
        "exch_ts": exch if exch is not None else prev.get("exch_ts"),
        "tick": tick,
        "min_order_size": minimum,
        "neg_risk": meta.get("neg_risk", prev.get("neg_risk")),
    }


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
                                apply_book(ev["asset_id"], ev.get("bids", []), ev.get("asks", []), ev)
                            elif et == "price_change":
                                for pc in ev.get("price_changes", []):
                                    book = S.books.get(pc["asset_id"])
                                    if not book:
                                        continue
                                    side = book["bids"] if pc["side"] == "BUY" else book["asks"]
                                    price, size = float(pc["price"]), float(pc["size"])
                                    if size == 0:
                                        side.pop(price, None)
                                    else:
                                        side[price] = size
                                    book["t"] = book["exch_ts"] = time.time()
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
                        apply_book(tok, j.get("bids", []), j.get("asks", []), j)
                except Exception as exc:
                    jlog("events.jsonl", {"type": "book_rest_error", "err": repr(exc)[:200]})
            if mk.get("condition_id") and time.time() - mk.get("_clob_at", 0) > 30:
                mk["_clob_at"] = time.time()
                try:
                    async with session.get("https://clob.polymarket.com/clob-markets/" + mk["condition_id"],
                                           timeout=aiohttp.ClientTimeout(total=5)) as response:
                        info = await response.json(content_type=None)
                    fee = (info or {}).get("fd") or {}
                    mk["clob_tick"] = info.get("mts")
                    mk["clob_min_size"] = info.get("mos")
                    if fee.get("r") is not None and abs(float(fee["r"]) - float(mk.get("fee_rate") or 0)) > 0.0001:
                        mk["fee_conflict"] = True
                        jlog("events.jsonl", {"type": "anomaly", "kind": "fee_conflict", "slug": mk["slug"],
                                              "gamma": mk.get("fee_rate"), "clob": fee.get("r")})
                    else:
                        mk["fee_conflict"] = False
                    mk["taker_delay"] = bool(info.get("itode"))
                except Exception as exc:
                    jlog("events.jsonl", {"type": "clob_market_error", "err": repr(exc)[:200]})
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


def _sync():
    if S.book is None:
        return
    S.positions = list(S.book.positions)
    S.realized = S.book.realized
    S.n_trades = S.book.n_trades
    S.n_wins = S.book.n_wins


def _venue_tick(market, book):
    return gates.effective_tick((book or {}).get("tick"), market.get("tick"), market.get("clob_tick"))


def _venue_min(market, book):
    return gates.strictest_min_size((book or {}).get("min_order_size"), market.get("min_order_size"), market.get("clob_min_size"))


_last_eval_t = 0.0
def trader_eval(force=False):
    global _last_eval_t
    now = time.time()
    if not force and now - _last_eval_t < 0.25:
        return
    _last_eval_t = now
    if S.book is None:
        return
    cur = int(now) - int(now) % WIN
    mk = S.markets.get(cur)
    if not mk:
        return
    fair = compute_fair(mk)
    if not fair:
        S.last_eval = {"slug": mk["slug"], "status": "waiting (price-to-beat/sigma/feed)"}
        return
    secs_left, elapsed = mk["end"] - now, now - mk["start"]
    feed_fresh = gates.point_is_fresh(fair["data_ts"], S.spot_recv.get(fair["data_ts"]), now, TC["max_feed_lag_s"])
    feed_gap = bool(mk.get("feed_gap")) or gates.window_has_feed_gap(
        S.spot.keys(), mk["start"], int(now), TC["max_feed_lag_s"])
    rules_ok, rules_reason = gates.market_rules_ok(mk)
    if mk.get("fee_conflict"):
        rules_ok = False
    live_requested = bool(S.limits.get("requested"))
    attempts = int(S.limits.get("max_attempts") or 2)
    ev = {"slug": mk["slug"], "start": mk["start"], "end": mk["end"], "secs_left": round(secs_left, 1), **fair,
          "feed_lag": round(now - fair["data_ts"], 2), "sigma_ann": model.sec_to_ann(S.sigma), "sides": {},
          "mode": S.mode, "rules_ok": rules_ok}
    window_reasons = []
    for side, token, side_fair in (("Up", mk["up"], fair["p_up"]), ("Down", mk["down"], 1 - fair["p_up"])):
        book = S.books.get(token) or {}
        asks = sorted((book.get("asks") or {}).items())
        exch_age = (now - book["exch_ts"]) if book.get("exch_ts") else None
        block = S.book.entry_block(cur, side, 1 if not live_requested else attempts)
        occupied = side if block == "already_entered" else ("Down" if side == "Up" else "Up") if block == "opposite_side" else None
        plan = gates.evaluate_entry(
            fair=side_fair, asks=asks, fee_rate=mk["fee_rate"], fee_exp=mk["fee_exp"],
            max_price=TC["max_price"], edge_threshold=TC["edge_threshold"], size_usd=TC["size_usd"],
            min_shares=TC["min_shares"], tick=_venue_tick(mk, book), min_order_size=_venue_min(mk, book),
            book_age_s=(now - book["t"]) if book.get("t") else None, book_exchange_age_s=exch_age,
            max_book_age_s=TC["max_book_age_s"], feed_fresh=feed_fresh, ptb_fresh=bool(mk.get("ptb_fresh")),
            feed_gap=feed_gap, secs_left=secs_left, min_secs_left=TC["min_secs_left"], elapsed=elapsed,
            min_secs_elapsed=TC["min_secs_elapsed"], side=side, occupied_side=occupied,
            kill=S.book.kill_latched(S.limits.get("kill_env", False)), rules_ok=rules_ok, accepting=mk.get("accepting"),
            live=live_requested, live_entry_allowed=bool(S.engine and S.engine.entry_allowed),
            open_cost_usd=S.book.open_cost(), reserved_usd=S.book.reserved_cost(),
            max_open_cost_usd=float(S.limits.get("max_open_cost_usd") or 0),
        )
        ev["sides"][side] = {
            "fair": side_fair, "ask": plan["best_ask"], "ask_size": plan["best_ask_size"], "bid": max(book["bids"]) if book.get("bids") else None,
            "fee_ps": plan["fee_per_share"], "edge": plan["edge"], "book_age": round(now - book["t"], 1) if book.get("t") else None,
            "status": plan["action"] if plan["action"] == "enter" else ",".join(plan["reasons"]),
        }
        sig = {"type": "signal", "slug": mk["slug"], "side": side, "best_ask": plan["best_ask"],
               "fee_per_share": None if plan["fee_per_share"] is None else round(plan["fee_per_share"], 5),
               "fair_prob": round(side_fair, 4), "edge": None if plan["edge"] is None else round(plan["edge"], 4),
               "fillable_shares": round(plan["shares"], 4), "fillable_usd": round(plan["cost_usd"], 4),
               "secs_left": round(secs_left, 1), "feed_lag": round(now - fair["data_ts"], 2),
               "spot": fair["spot"], "twap": fair["twap"], "price_to_beat": fair["K"], "method": fair["method"],
               "sigma_ann": round(model.sec_to_ann(S.sigma), 3), "blocked": plan["reasons"], "rules": rules_reason}
        if plan["action"] != "enter":
            key = (cur, side, tuple(plan["reasons"]))
            if plan["reasons"] and S.last_eval.get("_sigkey") != key and any(
                reason in plan["reasons"] for reason in ("insufficient_depth", "opposite_side", "edge", "price_cap", "feed_stale", "ptb_stale", "book_stale")
            ):
                jlog("signals.jsonl", sig)
                S.last_eval["_sigkey"] = key
            window_reasons.extend(plan["reasons"])
            continue
        jlog("signals.jsonl", sig)
        if live_requested:
            intent = S.book.begin_intent({
                "slug": mk["slug"], "start": cur, "end": mk["end"], "side": side, "token_id": token,
                "condition_id": mk.get("condition_id"), "stake_usd": TC["size_usd"], "limit_price": plan["limit_price"],
                "fee_rate": mk["fee_rate"], "fee_exp": mk["fee_exp"], "fair_prob": round(side_fair, 4),
                "edge": round(plan["net_edge"], 4), "price_to_beat": fair["K"], "secs_left": round(secs_left, 1),
                "sigma_ann": round(model.sec_to_ann(S.sigma), 3), "entry_time_la": la(now),
            }, attempts)
            if intent:
                S.pending_submits.append(intent["id"])
                jlog("trades.jsonl", {"type": "live_intent", "id": intent["id"], "slug": mk["slug"], "side": side,
                                      "limit_price": plan["limit_price"], "stake_usd": TC["size_usd"]})
            continue
        shares = plan["shares"]
        pos = {"slug": mk["slug"], "start": cur, "end": mk["end"], "side": side, "shares": round(shares, 4),
               "ask_vwap": round(plan["vwap"], 4), "best_ask": plan["best_ask"], "cost_usd": round(plan["cost_usd"], 4),
               "fee_usd": round(plan["fee_usd"], 5), "fee_per_share": round(plan["fee_usd"] / shares, 5),
               "fair_prob": round(side_fair, 4), "edge": round(plan["net_edge"], 4), "price_to_beat": fair["K"],
               "entry_ts": now, "entry_time_la": la(now), "secs_left_at_entry": round(secs_left, 1),
               "sigma_ann": round(model.sec_to_ann(S.sigma), 3), "partial": False}
        S.book.record_paper(pos)
        _sync()
        left = shares
        for price, size in list(book.get("asks", {}).items()):
            if left <= 0:
                break
            take = min(size, left)
            book["asks"][price] = size - take
            left -= take
            if book["asks"][price] <= 1e-9:
                book["asks"].pop(price, None)
        jlog("trades.jsonl", {"type": "paper_entry", **pos})
    ev["blocked"] = sorted(set(window_reasons))
    S.last_eval = {**ev, "_sigkey": S.last_eval.get("_sigkey")}


def _gamma_backoff(age_s: float) -> float:
    return 15 if age_s < 1800 else 60


# ---------------------------------------------------------------- settlement
async def settle_loop(session):
    while not S.shutdown:
        now = time.time()
        for mk in list(S.markets.values()):
            if mk["end"] > now or mk.get("feed_noted"):
                continue
            if mk["end"] in S.twap:
                mk["feed_final"] = S.twap[mk["end"]]
                anchor = mk.get("ptb")
                mk["feed_outcome"] = None if anchor is None else ("Up" if mk["feed_final"] >= anchor else "Down")
                mk["feed_noted"] = True
                jlog("events.jsonl", {"type": "window_feed_observation", "slug": mk["slug"], "ptb": anchor,
                                      "final": mk["feed_final"], "outcome": mk["feed_outcome"],
                                      "note": "provisional Chainlink observation; not a settlement"})
            elif now - mk["end"] > 30 and not mk.get("gap_logged"):
                mk["gap_logged"] = True
                jlog("events.jsonl", {"type": "anomaly", "kind": "no_twap_at_end", "slug": mk["slug"]})
        if S.book is not None:
            for pos in list(S.book.positions):
                end = pos.get("end")
                start = pos.get("start")
                if end is None or end > now or now - end < 20 or start is None:
                    continue
                mk = S.markets.get(start)
                if mk and now - mk.get("_last_gamma", 0) < _gamma_backoff(now - end):
                    continue
                if mk:
                    mk["_last_gamma"] = now
                try:
                    ev = await fetch_event(session, int(start))
                except Exception as exc:
                    jlog("events.jsonl", {"type": "gamma_error", "start": start, "err": repr(exc)[:200]})
                    continue
                if not ev:
                    continue
                if start not in S.markets:
                    ev["ptb"] = pos.get("price_to_beat")
                    ev["ptb_src"] = "restored"
                    ev["ptb_fresh"] = False
                    ev["ptb_locked"] = True
                    S.markets[start] = ev
                    mk = ev
                outcome, source = settlement.official_outcome(ev.get("closed"), ev.get("outcomePrices"), ev.get("meta"))
                meta = ev.get("meta") or {}
                if outcome is None:
                    if source == "official_conflict" and not (mk or {}).get("_conflict_logged"):
                        if mk is not None:
                            mk["_conflict_logged"] = True
                        jlog("events.jsonl", {"type": "anomaly", "kind": "official_conflict", "slug": pos.get("slug")})
                    continue
                feed_outcome = (mk or {}).get("feed_outcome")
                if feed_outcome and feed_outcome != outcome:
                    jlog("events.jsonl", {"type": "anomaly", "kind": "settle_mismatch", "slug": pos.get("slug"),
                                          "feed": feed_outcome, "official": outcome})
                official_ptb = meta.get("priceToBeat")
                if official_ptb and pos.get("price_to_beat") and abs(float(official_ptb) - float(pos["price_to_beat"])) > 0.01:
                    jlog("events.jsonl", {"type": "anomaly", "kind": "ptb_mismatch", "slug": pos.get("slug"),
                                          "ours": pos.get("price_to_beat"), "official": official_ptb})
                row = S.book.settle_official(pos, outcome, meta.get("finalPrice"), source, la())
                if row is None:
                    continue
                kind = "live_settle" if pos.get("mode") == "live" else "paper_settle"
                jlog("trades.jsonl", {"type": kind, **row})
                jlog("events.jsonl", {"type": "window_final", "slug": pos.get("slug"), "outcome": outcome, "src": source,
                                      "official_final": meta.get("finalPrice"), "official_ptb": official_ptb})
                S.book.flush_csv(os.path.join(LOG, "trades.csv"))
                _sync()
        await asyncio.sleep(1)


# ---------------------------------------------------------------- housekeeping / status
async def housekeeping():
    last_tick = 0
    while not S.shutdown:
        now = time.time()
        update_sigma(); prune(); trader_eval(force=True)
        if S.last_feed_wall and now - S.last_feed_wall > TC["max_feed_lag_s"]:
            _taint_gap(S.last_feed_wall, now)
        if S.last_feed_wall and now - S.last_feed_wall > 10 and now - S.last_gap_logged > 60:
            S.last_gap_logged = now
            jlog("events.jsonl", {"type": "anomaly", "kind": "feed_gap", "secs": round(now - S.last_feed_wall, 1)})
        snapshot = S.last_eval
        if now - last_tick >= AC["tick_log_every_s"] and snapshot.get("sides"):
            last_tick = now
            jlog("ticks.jsonl", {k: snapshot.get(k) for k in ("slug", "secs_left", "K", "spot", "spot_now", "cb", "twap", "tau", "p_up",
                                                              "method", "feed_lag", "sigma_ann", "sides", "blocked", "mode")})
        _sync()
        intents = []
        if S.book is not None:
            intents = [{"id": item.get("id"), "slug": item.get("slug"), "side": item.get("side"), "status": item.get("status"),
                        "shares": item.get("shares"), "cost_usd": item.get("cost_usd"), "order_id": item.get("order_id")}
                       for item in S.book.intents if item.get("status") not in {"rejected", "absent", "unmatched", "not_sent"}]
        status = {"updated_la": la(), "uptime_s": int(now - S.started), "eval": {k: v for k, v in snapshot.items() if k != "_sigkey"},
                  "sigma_src": S.sigma_src, "open_positions": S.positions, "realized_pnl": round(S.realized, 4),
                  "n_trades": S.n_trades, "n_wins": S.n_wins, "feed_age_s": round(now - S.last_feed_wall, 1) if S.last_feed_wall else None,
                  "markets": sorted(m["slug"] for m in S.markets.values()), "pid": os.getpid(), "mode": S.mode,
                  "live_reason": S.live_reason, "kill": bool(S.book and S.book.kill), "open_intents": intents}
        persist.atomic_write(os.path.join(LOG, "state.json"), status)
        await asyncio.sleep(1)


async def execution_loop():
    while not S.shutdown:
        if S.engine is not None and S.engine.broker is not None and S.limits.get("ready"):
            await S.engine.refresh_eligibility()
            S.live_reason = S.engine.block_reason
            S.mode = "live" if S.engine.entry_allowed else "live_blocked"
        if S.pending_submits and S.engine is not None:
            intent_id = S.pending_submits.pop(0)
            result = await S.engine.submit(intent_id)
            _sync()
            jlog("trades.jsonl", {"type": "live_submit", "id": intent_id, "status": result.get("status"),
                                  "reason": result.get("reason"), "order_id": result.get("order_id"),
                                  "shares": result.get("shares"), "cost_usd": result.get("cost_usd"),
                                  "cost_basis": result.get("cost_basis")})
        else:
            await asyncio.sleep(0.2)


def configure_runtime():
    persist.load_env_file(os.path.join(HERE, ".env"))
    S.secrets = live_exec.secret_values(os.environ)
    S.limits = live_exec.live_config(CFG, os.environ)
    S.book = live_exec.ExecutionBook(os.path.join(LOG, "execution_state.json"), os.path.join(LOG, "KILL"))
    if S.book.load():
        S.book.flush_csv(os.path.join(LOG, "trades.csv"))
    else:
        restored = settlement.restore_from_logs(LOG)
        S.book.positions = restored["positions"]
        S.book.realized = restored["realized"]
        S.book.n_trades = restored["n_trades"]
        S.book.n_wins = restored["n_wins"]
        S.book.save()
    S.book.kill_latched(bool(S.limits.get("kill_env")))
    _sync()


async def _arm_live():
    if not S.limits.get("requested"):
        S.mode = "paper"
        S.live_reason = "live_disabled"
        S.engine = live_exec.Engine(S.book, None, S.limits)
        return None
    if not S.limits.get("ready"):
        S.mode = "live_blocked"
        S.live_reason = ",".join(S.limits.get("reasons") or ["not_ready"])
        S.engine = live_exec.Engine(S.book, None, S.limits)
        S.engine.entry_allowed = False
        S.engine.block_reason = S.live_reason
        return None
    broker = live_exec.PolymarketBroker(os.environ)
    S.engine = live_exec.Engine(S.book, broker, S.limits)
    try:
        await broker.connect()
    except Exception as exc:
        S.engine.entry_allowed = False
        S.engine.block_reason = "client_connect_failed"
        jlog("events.jsonl", {"type": "live_connect_failed", "err": type(exc).__name__})
    if S.book.kill:
        S.engine.entry_allowed = False
        S.engine.block_reason = "kill_switch"
    else:
        await S.engine.refresh_eligibility(force=True)
    S.mode = "live" if S.engine.entry_allowed else "live_blocked"
    S.live_reason = S.engine.block_reason
    await S.engine.reconcile_open()
    _sync()
    return broker


async def _shutdown(broker):
    S.shutdown = True
    if broker is not None and S.engine is not None:
        try:
            await S.engine.cancel_resting()
        except Exception as exc:
            jlog("events.jsonl", {"type": "cancel_failed", "err": type(exc).__name__})
        await broker.close()
    if S.book is not None:
        S.book.save()


async def main():
    configure_runtime()
    broker = await _arm_live()
    jlog("events.jsonl", {"type": "start", "pid": os.getpid(), "mode": S.mode, "live_reason": S.live_reason,
                          "config_live_enabled": bool((CFG.get("live") or {}).get("enabled", False)),
                          "restored_open": len(S.positions), "kill": bool(S.book.kill)})
    async with aiohttp.ClientSession(trust_env=False, headers={"User-Agent": "polybtc/1.0"}) as session:
        await bootstrap_sigma(session)
        for pos in list(S.positions):
            if pos.get("start") in (None, "") or pos["start"] in S.markets:
                continue
            try:
                ev = await fetch_event(session, int(pos["start"]))
                if ev:
                    ev["ptb"] = pos.get("price_to_beat")
                    ev["ptb_src"] = "restored"
                    ev["ptb_fresh"] = False
                    ev["ptb_locked"] = True
                    S.markets[pos["start"]] = ev
            except Exception:
                pass
        tasks = [asyncio.create_task(coro) for coro in (
            rtds_loop(), coinbase_loop(), discovery_loop(session), book_ws_loop(), book_rest_loop(session),
            settle_loop(session), housekeeping(), execution_loop())]
        try:
            while not S.shutdown:
                await asyncio.sleep(0.2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _shutdown(broker)


async def cancel_all():
    configure_runtime()
    if not S.limits.get("ready"):
        print("Live mode is not configured. No order was sent and no cancel was sent.")
        return 2
    S.book.kill = True
    S.kill_path_written = S.book.kill_path.write_text("kill\n", encoding="utf-8")
    S.book.save()
    broker = await _arm_live()
    if broker is None:
        print("Live client was not created. No cancel was sent.")
        return 2
    result = await S.engine.cancel_resting()
    await S.engine.reconcile_open()
    await broker.close()
    print("Cancel requested. New entries stay off until logs/KILL is removed.")
    print("canceled" if result.get("ok") else "cancel_failed")
    return 0 if result.get("ok") else 1


def _install_signals():
    def handle(*_args):
        S.shutdown = True
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handle)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="polybtc BTC 5-minute watcher. Paper unless live mode is enabled locally.")
    parser.add_argument("--cancel-all", action="store_true", help="Cancel resting account orders and latch the kill switch. Places nothing.")
    args = parser.parse_args()
    _install_signals()
    if args.cancel_all:
        sys.exit(asyncio.run(cancel_all()))
    asyncio.run(main())
