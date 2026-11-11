"""Entry gates for BTC 5-minute Up/Down. Pure functions; no orders and no model changes.

The probability model in model.py is not modified here. Fees use that module's
formula, which matches the published taker fee when the market exponent is 1:
fee = shares * rate * p * (1 - p), rounded to 5 decimal places in USDC.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import model

DUST_USD = Decimal("0.01")
OFFICIAL_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "SOCKS_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "socks_proxy",
)


def _d(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def quantize_fee(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def fee_per_share(price: Decimal, rate: Decimal, exponent: Decimal) -> Decimal:
    """Same formula as model.fee_per_share, evaluated in Decimal for the order gate."""
    base = price * (Decimal(1) - price)
    if exponent == 1:
        return rate * base
    return rate * (base ** exponent)


def fee_usd(shares: Decimal, price: Decimal, rate: Decimal, exponent: Decimal) -> Decimal:
    return quantize_fee(shares * fee_per_share(price, rate, exponent))


def on_tick(price: Decimal, tick: Decimal) -> bool:
    if tick <= 0:
        return False
    steps = price / tick
    return abs(steps - steps.to_integral_value()) <= Decimal("0.0000001")


def effective_tick(*values) -> Decimal | None:
    """Coarsest tick that still conforms to every known tick, or None if they conflict.

    A 0.01 book tick and a 0.001 CLOB minimum tick are compatible: 0.01 is used.
    """
    ticks: list[Decimal] = []
    for value in values:
        tick = _d(value)
        if tick is None or tick <= 0:
            continue
        ticks.append(tick)
    if not ticks:
        return None
    coarse = max(ticks)
    for tick in ticks:
        ratio = coarse / tick
        if abs(ratio - ratio.to_integral_value()) > Decimal("0.000001"):
            return None
    return coarse


def strictest_min_size(*values) -> Decimal | None:
    sizes = [item for item in (_d(v) for v in values) if item is not None and item > 0]
    if not sizes:
        return None
    return max(sizes)


def ptb_is_fresh(start: int, received_wall: float | None, max_lag_s: float) -> bool:
    """True only when the start-second TWAP was received live, not backfilled later."""
    if received_wall is None:
        return False
    return (start - 2) <= float(received_wall) <= start + float(max_lag_s) + 2


def point_is_fresh(data_ts: float | None, received_wall: float | None, now: float, max_lag_s: float) -> bool:
    if data_ts is None or received_wall is None:
        return False
    if now - float(data_ts) > max_lag_s:
        return False
    if now - float(received_wall) > max_lag_s:
        return False
    if float(received_wall) - float(data_ts) > max_lag_s + 2:
        return False
    return True


def window_has_feed_gap(keys, window_start: int, now_s: int, max_gap_s: float) -> bool:
    """True when this window's spot series has a hole longer than the staleness limit.

    The hole stays visible after the feed recovers, so the window is not traded
    on a price that skipped the gap.
    """
    inside = sorted(int(k) for k in keys if window_start <= int(k) <= now_s)
    if not inside:
        return True
    if inside[0] - window_start > max_gap_s:
        return True
    prev = inside[0]
    for key in inside[1:]:
        if key - prev > max_gap_s:
            return True
        prev = key
    if now_s - inside[-1] > max_gap_s:
        return True
    return False


def market_rules_ok(market: dict) -> tuple[bool, str]:
    """BTC 5-minute Chainlink TWAP60 Up/Down only. Anything else is not traded."""
    start = market.get("start")
    slug = market.get("slug") or ""
    if start is None or slug != f"btc-updown-5m-{int(start)}":
        return False, "slug"
    if int(market.get("end") or 0) - int(start) != 300:
        return False, "duration"
    version = str(market.get("version") or "v1")
    if version.startswith("v2"):
        return False, "version"
    source = str(market.get("resolution_source") or "")
    if source:
        lowered = source.lower()
        if "chain.link" not in lowered or "twap" not in lowered:
            return False, "resolution_source"
    config = market.get("crypto_config") or {}
    if config:
        asset = str(config.get("asset") or "").lower()
        duration = str(config.get("duration") or "")
        lookback = config.get("twapLookbackSeconds")
        if asset != "btc" or duration != "5m":
            return False, "crypto_config"
        if config.get("twapEnabled") is False:
            return False, "twap_disabled"
        if lookback is not None and int(lookback) != 60:
            return False, "twap_lookback"
    if not market.get("up") or not market.get("down"):
        return False, "tokens"
    return True, "ok"


def geoblock_allows(payload, error: str | None = None) -> tuple[bool, str]:
    """Fail closed. Official flag is `blocked`. US is refused even if that flag were false."""
    if error:
        return False, "geoblock_unreachable"
    if not isinstance(payload, dict):
        return False, "geoblock_malformed"
    if "blocked" not in payload or not isinstance(payload.get("blocked"), bool):
        return False, "geoblock_malformed"
    if payload.get("blocked") is True:
        return False, "geoblock_blocked"
    country = str(payload.get("country") or "").strip().upper()
    if not country:
        return False, "geoblock_malformed"
    if country in {"US", "UM"}:
        return False, "geoblock_us"
    return True, "ok"


def proxy_env_set(environ: dict) -> list[str]:
    return [name for name in PROXY_ENV_NAMES if str(environ.get(name) or "").strip()]


def evaluate_entry(
    *,
    fair: float,
    asks: list[tuple[float, float]],
    fee_rate: float,
    fee_exp: float,
    max_price: float,
    edge_threshold: float,
    size_usd: float,
    min_shares: float,
    tick,
    min_order_size,
    book_age_s: float | None,
    book_exchange_age_s: float | None,
    max_book_age_s: float,
    feed_fresh: bool,
    ptb_fresh: bool,
    feed_gap: bool,
    secs_left: float,
    min_secs_left: float,
    elapsed: float,
    min_secs_elapsed: float,
    side: str,
    occupied_side: str | None,
    kill: bool,
    rules_ok: bool,
    accepting,
    live: bool,
    live_entry_allowed: bool,
    open_cost_usd: float = 0.0,
    reserved_usd: float = 0.0,
    max_open_cost_usd: float = 0.0,
) -> dict:
    """Decide whether one side may be entered. A skip never returns a reduced stake."""
    rate = Decimal(str(fee_rate))
    exponent = Decimal(str(fee_exp))
    cap = Decimal(str(max_price))
    edge_need = Decimal(str(edge_threshold))
    stake = Decimal(str(size_usd))
    fair_d = Decimal(str(fair))
    tick_d = _d(tick)
    min_size = strictest_min_size(min_order_size, min_shares)
    reasons: list[str] = []
    if kill:
        reasons.append("kill_switch")
    if not rules_ok:
        reasons.append("market_rules")
    if feed_gap or not feed_fresh:
        reasons.append("feed_stale")
    if not ptb_fresh:
        reasons.append("ptb_stale")
    if secs_left < min_secs_left:
        reasons.append("too_late")
    if elapsed < min_secs_elapsed:
        reasons.append("too_early")
    if book_age_s is None or book_age_s > max_book_age_s:
        reasons.append("book_stale")
    elif book_exchange_age_s is not None and book_exchange_age_s > max_book_age_s:
        reasons.append("book_stale")
    if accepting is False:
        reasons.append("not_accepting")
    if live and not live_entry_allowed:
        reasons.append("live_not_allowed")
    if occupied_side and occupied_side != side:
        reasons.append("opposite_side")
    elif occupied_side == side:
        reasons.append("already_entered")
    if live and stake > 0 and (Decimal(str(open_cost_usd)) + Decimal(str(reserved_usd)) + stake) > Decimal(str(max_open_cost_usd)):
        reasons.append("exposure_cap")
    if live and (tick_d is None or _d(min_order_size) is None):
        reasons.append("constraints_unknown")

    best = None
    best_edge = None
    best_fee = None
    best_size = None
    limit_price = None
    shares = Decimal(0)
    cost = Decimal(0)
    fee = Decimal(0)
    saw_off_tick = False
    asks = sorted(asks, key=lambda item: (item[0] is None, item[0]))
    for raw_price, raw_size in asks:
        price = _d(raw_price)
        size = _d(raw_size)
        if price is None or size is None or size <= 0 or price <= 0 or price >= 1:
            continue
        if tick_d is not None and not on_tick(price, tick_d):
            saw_off_tick = True
            break
        if price > cap:
            break
        per = fee_per_share(price, rate, exponent)
        edge = fair_d - (price + per)
        if best is None:
            best = price
            best_edge = edge
            best_fee = per
            best_size = size
        if edge < edge_need:
            break
        room = stake - cost
        if room <= 0:
            break
        take = min(size, room / price)
        shares += take
        cost += take * price
        fee += take * per
        limit_price = price
        if cost + DUST_USD >= stake:
            break
    fee = quantize_fee(fee)
    if best is None and not asks:
        level_reason = "no_book"
    elif best is None and asks and _d(asks[0][0]) is not None and _d(asks[0][0]) > cap:
        level_reason = "price_cap"
    elif best is not None and best > cap:
        level_reason = "price_cap"
    elif best_edge is not None and best_edge < edge_need:
        level_reason = "edge"
    elif saw_off_tick and cost + DUST_USD < stake:
        level_reason = "off_tick"
    elif cost + DUST_USD < stake:
        level_reason = "insufficient_depth"
    elif min_size is not None and shares < min_size:
        level_reason = "below_min_size"
    else:
        level_reason = None
    if level_reason:
        reasons.append(level_reason)
    # Never intentionally size below the configured stake.
    if cost + DUST_USD < stake:
        shares = Decimal(0)
        cost = Decimal(0)
        fee = Decimal(0)
        limit_price = None
    vwap = (cost / shares) if shares > 0 else None
    net_edge = (fair_d - vwap - (fee / shares)) if shares > 0 else best_edge
    action = "enter" if not reasons and shares > 0 and cost + DUST_USD >= stake else "skip"
    return {
        "action": action,
        "reasons": reasons,
        "shares": float(shares),
        "cost_usd": float(cost),
        "fee_usd": float(fee),
        "vwap": float(vwap) if vwap is not None else None,
        "best_ask": float(best) if best is not None else None,
        "best_ask_size": float(best_size) if best_size is not None else None,
        "edge": float(best_edge) if best_edge is not None else None,
        "net_edge": float(net_edge) if net_edge is not None else None,
        "fee_per_share": float(best_fee) if best_fee is not None else None,
        "limit_price": float(limit_price) if limit_price is not None else None,
    }


def reference_fee_matches_model() -> bool:
    """Guard: the gate fee and model.fee_per_share stay the same formula."""
    price = 0.7
    rate = 0.07
    got = float(fee_per_share(Decimal(str(price)), Decimal(str(rate)), Decimal(1)))
    return abs(got - model.fee_per_share(price, rate, 1)) < 1e-12
