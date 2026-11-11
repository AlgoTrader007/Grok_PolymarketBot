"""Live-order adapter for the Polymarket international CLOB.

Default is paper. Live orders are sent only when config, the confirmation
environment variable, local credentials, a passing geoblock check, and account
closed-only status all allow it. Proxy variables and a failed eligibility
check refuse new orders. This module does not read secrets from the repository.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal
from pathlib import Path

import gates
import persist
import settlement

CONFIRM_VALUE = "YES"
LIVE_ENV_NAMES = (
    "POLYBTC_LIVE_CONFIRM",
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_WALLET_ADDRESS",
    "POLYMARKET_RELAYER_API_KEY",
    "POLYMARKET_RELAYER_API_KEY_ADDRESS",
    "POLYMARKET_BUILDER_API_KEY",
    "POLYMARKET_BUILDER_SECRET",
    "POLYMARKET_BUILDER_PASSPHRASE",
    "POLYBTC_KILL",
)
_SCALE = Decimal("1000000")


class AmbiguousSubmit(Exception):
    """The POST may have reached the exchange. Do not send another order yet."""


class NotSent(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def live_config(cfg: dict, environ: dict) -> dict:
    """Decide whether live mode was requested and whether it is allowed to arm.

    `ready` means local configuration is complete. Geoblock and closed-only are
    checked separately on the network and can still refuse orders.
    """
    live = cfg.get("live") or {}
    enabled = bool(live.get("enabled", False))
    confirm = str(environ.get("POLYBTC_LIVE_CONFIRM") or "")
    proxies = gates.proxy_env_set(environ)
    reasons = []
    if not enabled:
        reasons.append("live_disabled")
    if confirm != CONFIRM_VALUE:
        reasons.append("confirm_missing")
    key = str(environ.get("POLYMARKET_PRIVATE_KEY") or "")
    wallet = str(environ.get("POLYMARKET_WALLET_ADDRESS") or "")
    if enabled and confirm == CONFIRM_VALUE:
        if not key:
            reasons.append("private_key_missing")
        if not wallet:
            reasons.append("wallet_missing")
        if proxies:
            reasons.append("proxy_env")
    requested = enabled and confirm == CONFIRM_VALUE
    ready = requested and not any(
        item in reasons for item in ("private_key_missing", "wallet_missing", "proxy_env")
    )
    return {
        "enabled": enabled,
        "requested": requested,
        "ready": ready,
        "reasons": reasons,
        "order_type": str(live.get("order_type") or "FAK"),
        "max_open_cost_usd": float(live.get("max_open_cost_usd", 75)),
        "max_attempts": int(live.get("max_attempts_per_window", 2)),
        "submit_timeout_s": float(live.get("submit_timeout_s", 20)),
        "geoblock_max_age_s": float(live.get("geoblock_max_age_s", 30)),
        "kill_env": str(environ.get("POLYBTC_KILL") or "") in {"1", "true", "TRUE", "yes", "YES"},
        "has_relayer": bool(environ.get("POLYMARKET_RELAYER_API_KEY") and environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS")),
        "has_builder": bool(
            environ.get("POLYMARKET_BUILDER_API_KEY")
            and environ.get("POLYMARKET_BUILDER_SECRET")
            and environ.get("POLYMARKET_BUILDER_PASSPHRASE")
        ),
    }


def secret_values(environ: dict) -> list[str]:
    values = []
    for name in LIVE_ENV_NAMES:
        value = environ.get(name)
        if value and name != "POLYBTC_LIVE_CONFIRM" and name != "POLYBTC_KILL":
            values.append(str(value))
    return values


def interpret_buy_amounts(making, taking, signed_cost: Decimal, limit_price: Decimal) -> dict | None:
    """Map a CLOB amount pair to human USDC and shares.

    The HTTP body uses 6-decimal integers. The SDK exposes those strings as
    Decimal without scaling. A human $15 fill is at or below the signed stake;
    the same fill in base units is 15000000. Zero means no fill amounts were
    reported, which is not by itself a confirmed zero-fill.
    """
    try:
        making_d = Decimal(str(making))
        taking_d = Decimal(str(taking))
    except Exception:
        return None
    if making_d == 0 and taking_d == 0:
        return {"shares": Decimal(0), "cost_usd": Decimal(0), "price": None, "basis": "zero"}
    if making_d > signed_cost + Decimal(1):
        cost = making_d / _SCALE
        shares = taking_d / _SCALE
        basis = "e6"
    else:
        cost = making_d
        shares = taking_d
        basis = "human"
    if shares <= 0 or cost < 0:
        return None
    price = cost / shares
    if price <= 0 or price > limit_price + Decimal("0.000001") or price >= 1:
        return None
    if cost > signed_cost + Decimal("0.05"):
        return None
    return {"shares": shares, "cost_usd": cost, "price": price, "basis": basis}


class ExecutionBook:
    """Durable intents and positions. Money fields are floats rounded to 6 dp."""

    def __init__(self, path: str | Path, kill_path: str | Path | None = None):
        self.path = Path(path)
        self.kill_path = Path(kill_path) if kill_path else self.path.parent / "KILL"
        self.intents: list[dict] = []
        self.positions: list[dict] = []
        self.pending_csv: list[dict] = []
        self.realized = 0.0
        self.n_trades = 0
        self.n_wins = 0
        self.kill = False

    def load(self) -> bool:
        data = persist.read_json(self.path, default=None)
        if not isinstance(data, dict):
            return False
        self.intents = list(data.get("intents") or [])
        self.positions = list(data.get("positions") or [])
        self.pending_csv = list(data.get("pending_csv") or [])
        self.realized = float(data.get("realized") or 0)
        self.n_trades = int(data.get("n_trades") or 0)
        self.n_wins = int(data.get("n_wins") or 0)
        self.kill = bool(data.get("kill"))
        return True

    def save(self) -> None:
        persist.atomic_write(self.path, {
            "version": 1,
            "intents": self.intents,
            "positions": self.positions,
            "pending_csv": self.pending_csv,
            "realized": round(self.realized, 4),
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "kill": self.kill,
        })

    def kill_latched(self, env_kill: bool = False) -> bool:
        file_kill = self.kill_path.exists()
        if file_kill or env_kill:
            self.kill = True
        return self.kill or file_kill or env_kill

    def entry_block(self, start: int, side: str, max_attempts: int) -> str | None:
        """None when an entry on this side is still allowed.

        Any exchange attempt locks the window to that side. A confirmed zero-fill
        can be retried on the same side until max_attempts. An in-flight, unknown,
        or filled attempt cannot.
        """
        positions = [
            item for item in self.positions
            if int(item.get("start") or -1) == int(start) and float(item.get("shares") or 0) > 0
        ]
        if positions:
            return "already_entered" if positions[0].get("side") == side else "opposite_side"
        now = time.time()
        recent_local = [
            item for item in self.intents
            if int(item.get("start") or -1) == int(start) and item.get("status") == "not_sent"
            and now - float(item.get("created_ts") or 0) < 30
        ]
        if any(item.get("side") != side for item in recent_local):
            return "opposite_side"
        if any(item.get("side") == side for item in recent_local):
            return "already_entered"
        intents = [
            item for item in self.intents
            if int(item.get("start") or -1) == int(start) and item.get("status") != "not_sent"
        ]
        if not intents:
            return None
        if any(item.get("side") != side for item in intents):
            return "opposite_side"
        inflight = {"reserved", "submitting", "submit_unknown", "delayed", "accepted"}
        if any(item.get("status") in inflight or float(item.get("shares") or 0) > 0 for item in intents):
            return "already_entered"
        zeros = {"rejected", "unmatched", "absent"}
        if any(item.get("status") not in zeros for item in intents):
            return "already_entered"
        if len(intents) >= max_attempts:
            return "already_entered"
        return None

    def open_cost(self) -> float:
        return round(sum(float(p.get("cost_usd") or 0) for p in self.positions), 6)

    def reserved_cost(self) -> float:
        total = 0.0
        for intent in self.intents:
            if intent.get("status") in {"reserved", "submitting", "submit_unknown", "delayed", "accepted"} and float(intent.get("shares") or 0) <= 0:
                total += float(intent.get("stake_usd") or 0)
        return round(total, 6)

    def _intent(self, intent_id: str) -> dict:
        for intent in self.intents:
            if intent.get("id") == intent_id:
                return intent
        raise KeyError(intent_id)

    def begin_intent(self, spec: dict, max_attempts: int) -> dict | None:
        start = int(spec["start"])
        side = spec["side"]
        if self.kill_latched():
            return None
        if self.entry_block(start, side, max_attempts):
            return None
        intent = {
            "id": uuid.uuid4().hex,
            "slug": spec["slug"],
            "start": start,
            "end": int(spec["end"]),
            "side": side,
            "token_id": str(spec["token_id"]),
            "condition_id": spec.get("condition_id"),
            "stake_usd": round(float(spec["stake_usd"]), 6),
            "limit_price": round(float(spec["limit_price"]), 6),
            "fee_rate": float(spec["fee_rate"]),
            "fee_exp": float(spec["fee_exp"]),
            "fair_prob": spec.get("fair_prob"),
            "edge": spec.get("edge"),
            "price_to_beat": spec.get("price_to_beat"),
            "secs_left_at_entry": spec.get("secs_left"),
            "sigma_ann": spec.get("sigma_ann"),
            "status": "reserved",
            "order_id": None,
            "shares": 0.0,
            "cost_usd": 0.0,
            "fee_usd": 0.0,
            "created_ts": time.time(),
            "entry_time_la": spec.get("entry_time_la"),
        }
        self.intents.append(intent)
        self.save()
        return intent

    def mark(self, intent_id: str, **fields) -> dict:
        intent = self._intent(intent_id)
        intent.update(fields)
        self.save()
        return intent

    def record_position_from_intent(self, intent_id: str) -> dict | None:
        intent = self._intent(intent_id)
        shares = float(intent.get("shares") or 0)
        if shares <= 0:
            return None
        if any(position.get("intent_id") == intent_id for position in self.positions):
            return next(position for position in self.positions if position.get("intent_id") == intent_id)
        cost = float(intent.get("cost_usd") or 0)
        fee = float(intent.get("fee_usd") or 0)
        position = {
            "intent_id": intent_id,
            "mode": "live",
            "slug": intent["slug"],
            "start": intent["start"],
            "end": intent["end"],
            "side": intent["side"],
            "token_id": intent["token_id"],
            "shares": round(shares, 6),
            "ask_vwap": round(cost / shares, 6) if shares else None,
            "best_ask": intent.get("limit_price"),
            "cost_usd": round(cost, 6),
            "fee_usd": round(fee, 6),
            "fee_per_share": round(fee / shares, 6) if shares else 0.0,
            "fair_prob": intent.get("fair_prob"),
            "edge": intent.get("edge"),
            "price_to_beat": intent.get("price_to_beat"),
            "entry_ts": intent.get("created_ts"),
            "entry_time_la": intent.get("entry_time_la"),
            "secs_left_at_entry": intent.get("secs_left_at_entry"),
            "sigma_ann": intent.get("sigma_ann"),
            "partial": cost + 0.01 < float(intent.get("stake_usd") or 0),
            "cost_basis": intent.get("cost_basis") or "actual",
            "order_id": intent.get("order_id"),
            "settlement": "unresolved",
        }
        self.positions.append(position)
        self.save()
        return position

    def record_paper(self, position: dict) -> dict:
        position = dict(position)
        position["mode"] = "paper"
        position["settlement"] = "unresolved"
        self.positions.append(position)
        self.save()
        return position

    def settle_official(self, position: dict, outcome: str, final_price, source: str, settle_time_la: str) -> dict | None:
        pnl = settlement.settlement_pnl(position, outcome)
        if pnl is None:
            return None
        key = settlement.position_key(position)
        self.positions = [item for item in self.positions if settlement.position_key(item) != key]
        row = {
            **position,
            "settle_time_la": settle_time_la,
            "final_price": final_price,
            "result": pnl["result"],
            "pnl_usd": pnl["pnl_usd"],
            "settle_source": source if source in settlement.OFFICIAL_SOURCES else "gamma_official",
        }
        self.realized = round(self.realized + pnl["pnl_usd"], 4)
        self.n_trades += 1
        self.n_wins += int(pnl["win"])
        self.pending_csv.append({name: row.get(name) for name in settlement.CSV_FIELDS})
        self.save()
        return row

    def flush_csv(self, csv_path: str | Path) -> int:
        written = 0
        remaining = []
        for row in self.pending_csv:
            if settlement.append_csv(csv_path, row):
                written += 1
            else:
                # Already recorded. Drop it so a restart cannot double-count.
                pass
        self.pending_csv = remaining
        self.save()
        return written


def _fee_for(shares: Decimal, cost: Decimal, rate: Decimal, exponent: Decimal) -> Decimal:
    if shares <= 0:
        return Decimal(0)
    price = cost / shares
    return gates.fee_usd(shares, price, rate, exponent)


class Engine:
    def __init__(self, book: ExecutionBook, broker, limits: dict):
        self.book = book
        self.broker = broker
        self.limits = limits
        self.entry_allowed = False
        self.block_reason = "not_checked"
        self._geo_mono = 0.0
        self._geo_ok = None
        self._geo_reason = "not_checked"

    def adopt_logs(self, restored: dict) -> None:
        if self.book.positions or self.book.intents:
            return
        self.book.positions = list(restored.get("positions") or [])
        self.book.realized = float(restored.get("realized") or 0)
        self.book.n_trades = int(restored.get("n_trades") or 0)
        self.book.n_wins = int(restored.get("n_wins") or 0)
        self.book.save()

    async def refresh_eligibility(self, force: bool = False) -> bool:
        if self.book.kill_latched(self.limits.get("kill_env", False)):
            self.entry_allowed = False
            self.block_reason = "kill_switch"
            return False
        if not self.limits.get("ready"):
            self.entry_allowed = False
            self.block_reason = ",".join(self.limits.get("reasons") or ["not_ready"])
            return False
        age = time.monotonic() - self._geo_mono
        if force or self._geo_ok is None or age > float(self.limits.get("geoblock_max_age_s") or 30):
            try:
                payload = await self.broker.geoblock()
                self._geo_ok, self._geo_reason = gates.geoblock_allows(payload)
            except Exception:
                self._geo_ok, self._geo_reason = False, "geoblock_unreachable"
            self._geo_mono = time.monotonic()
        if not self._geo_ok:
            self.entry_allowed = False
            self.block_reason = self._geo_reason
            return False
        if getattr(self.broker, "client", None) is None:
            self.entry_allowed = False
            self.block_reason = "client_not_connected"
            return False
        try:
            closed = await self.broker.closed_only()
        except Exception:
            self.entry_allowed = False
            self.block_reason = "closed_only_unknown"
            return False
        if closed:
            self.entry_allowed = False
            self.block_reason = "closed_only"
            return False
        self.entry_allowed = True
        self.block_reason = "ok"
        return True

    async def submit(self, intent_id: str) -> dict:
        intent = self.book._intent(intent_id)
        if self.book.kill_latched(self.limits.get("kill_env", False)):
            return self.book.mark(intent_id, status="not_sent", reason="kill_switch")
        allowed = await self.refresh_eligibility(force=False)
        if not allowed:
            return self.book.mark(intent_id, status="not_sent", reason=self.block_reason)
        try:
            prepared = await self.broker.prepare_buy(
                asset_id=intent["token_id"],
                amount=f"{float(intent['stake_usd']):.2f}",
                max_price=f"{float(intent['limit_price']):.4f}".rstrip("0").rstrip("."),
                order_type=self.limits.get("order_type") or "FAK",
            )
        except NotSent as exc:
            return self.book.mark(intent_id, status="not_sent", reason=exc.reason)
        except Exception as exc:
            return self.book.mark(intent_id, status="not_sent", reason=type(exc).__name__)
        self.book.mark(
            intent_id,
            status="submitting",
            salt=prepared.get("salt"),
            signed_maker=prepared.get("signed_maker"),
            signed_taker=prepared.get("signed_taker"),
            submit_started=time.time(),
        )
        try:
            result = await asyncio.wait_for(
                self.broker.post(prepared),
                timeout=float(self.limits.get("submit_timeout_s") or 20),
            )
        except NotSent as exc:
            return self.book.mark(intent_id, status="not_sent", reason=exc.reason)
        except (asyncio.TimeoutError, AmbiguousSubmit, TimeoutError):
            return self.book.mark(intent_id, status="submit_unknown", reason="submit_timeout")
        except Exception:
            return self.book.mark(intent_id, status="submit_unknown", reason="submit_error")
        return self._apply_result(intent_id, result)

    def _apply_result(self, intent_id: str, result: dict) -> dict:
        intent = self.book._intent(intent_id)
        disposition = result.get("disposition")
        if disposition == "rejected":
            return self.book.mark(
                intent_id,
                status="rejected" if result.get("order_id") is None else "unmatched",
                reason=str(result.get("error") or "rejected")[:180],
                order_id=result.get("order_id"),
            )
        if disposition == "ambiguous":
            return self.book.mark(intent_id, status="submit_unknown", order_id=result.get("order_id"), reason="ambiguous")
        status = str(result.get("status") or "").lower()
        order_id = result.get("order_id")
        if status in {"delayed", "live"}:
            return self.book.mark(intent_id, status="delayed" if status == "delayed" else "accepted", order_id=order_id)
        fill = result.get("fill")
        if status == "matched" and fill and Decimal(str(fill.get("shares") or 0)) > 0:
            return self._lock_fill(intent_id, fill, order_id, fill.get("basis") or "response")
        if status in {"unmatched", "rejected"} or (fill and Decimal(str(fill.get("shares") or 0)) == 0 and fill.get("basis") != "zero"):
            return self.book.mark(intent_id, status="unmatched", order_id=order_id, reason=status or "unmatched")
        if status == "matched":
            return self.book.mark(intent_id, status="delayed", order_id=order_id, reason="matched_without_amounts")
        return self.book.mark(intent_id, status="submit_unknown", order_id=order_id, reason="unrecognized_status")

    def _lock_fill(self, intent_id: str, fill: dict, order_id, basis: str) -> dict:
        intent = self.book._intent(intent_id)
        shares = Decimal(str(fill["shares"]))
        cost = Decimal(str(fill["cost_usd"]))
        fee = _fee_for(shares, cost, Decimal(str(intent["fee_rate"])), Decimal(str(intent["fee_exp"])))
        self.book.mark(
            intent_id,
            status="filled" if cost + Decimal("0.01") >= Decimal(str(intent["stake_usd"])) else "partial",
            order_id=order_id,
            shares=float(shares),
            cost_usd=float(cost),
            fee_usd=float(fee),
            cost_basis=basis,
        )
        self.book.record_position_from_intent(intent_id)
        return self.book._intent(intent_id)

    async def reconcile_open(self) -> None:
        """Resolve in-flight intents before any new submit. Unknown stays unknown."""
        for intent in list(self.book.intents):
            status = intent.get("status")
            if status == "reserved":
                self.book.mark(intent["id"], status="not_sent", reason="restart_before_submit")
                continue
            if status not in {"submitting", "submit_unknown", "delayed", "accepted"}:
                continue
            await self._reconcile_one(intent)

    async def _reconcile_one(self, intent: dict) -> None:
        intent_id = intent["id"]
        order_id = intent.get("order_id")
        try:
            if order_id:
                order = await self.broker.get_order(order_id)
            else:
                order = await self._match_open_order(intent)
        except Exception:
            self.book.mark(intent_id, status="submit_unknown", reason="reconcile_unavailable")
            return
        if order:
            shares = Decimal(str(order.get("size_matched") or 0))
            limit_price = Decimal(str(order.get("price") or intent.get("limit_price") or 0))
            order_status = str(order.get("status") or "").upper()
            if shares > 0:
                trades = await self._safe_trades(intent)
                actual = _trades_cost(trades, order.get("id") or order_id)
                if actual and actual["shares"] > 0:
                    self._lock_fill(intent_id, actual, order.get("id") or order_id, "trades")
                    return
                # Limit price is the worst price, so this cost is an upper bound until trades arrive.
                self._lock_fill(
                    intent_id,
                    {"shares": shares, "cost_usd": shares * limit_price},
                    order.get("id") or order_id,
                    "limit_price_cap",
                )
                return
            if order_status in {"CANCELED", "CANCELLED", "UNMATCHED", "REJECTED"}:
                self.book.mark(intent_id, status="unmatched", order_id=order.get("id") or order_id, reason=order_status)
                return
            if order_status in {"LIVE", "DELAYED", "OPEN"}:
                try:
                    await self.broker.cancel_all()
                except Exception:
                    pass
                self.book.mark(intent_id, status="submit_unknown", order_id=order.get("id") or order_id, reason="resting_cancel_requested")
                return
        trades = await self._safe_trades(intent)
        if trades is None:
            self.book.mark(intent_id, status="submit_unknown", reason="trades_unavailable")
            return
        grouped = _group_trades(trades, intent)
        if len(grouped) > 1:
            self.book.mark(intent_id, status="submit_unknown", reason="ambiguous_trades")
            return
        if len(grouped) == 1:
            only = next(iter(grouped.values()))
            self._lock_fill(intent_id, only, only.get("order_id"), "trades")
            return
        try:
            opens = await self.broker.list_open_orders(intent["token_id"])
        except Exception:
            self.book.mark(intent_id, status="submit_unknown", reason="open_orders_unavailable")
            return
        if opens:
            self.book.mark(intent_id, status="submit_unknown", reason="unmatched_open_order")
            return
        self.book.mark(intent_id, status="absent", reason="confirmed_not_placed")

    async def _match_open_order(self, intent: dict):
        orders = await self.broker.list_open_orders(intent["token_id"])
        signed_taker = intent.get("signed_taker")
        target = (Decimal(signed_taker) / _SCALE) if signed_taker else None
        hits = []
        for order in orders:
            if str(order.get("side") or "").upper() != "BUY":
                continue
            price = Decimal(str(order.get("price") or 0))
            if price - Decimal(str(intent["limit_price"])) > Decimal("0.000001"):
                continue
            if target is not None:
                original = Decimal(str(order.get("original_size") or 0))
                if abs(original - target) > Decimal("0.05"):
                    continue
            hits.append(order)
        if len(hits) == 1:
            return hits[0]
        return None

    async def _safe_trades(self, intent: dict):
        try:
            return await self.broker.list_trades(intent["token_id"], after=str(int(float(intent.get("submit_started") or intent.get("created_ts") or 0)) - 2))
        except Exception:
            return None

    async def cancel_resting(self) -> dict:
        try:
            result = await self.broker.cancel_all()
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
        for intent in self.book.intents:
            if intent.get("status") in {"delayed", "accepted"} and float(intent.get("shares") or 0) <= 0:
                intent["status"] = "submit_unknown"
                intent["reason"] = "cancel_requested"
        self.book.save()
        return {"ok": True, "result": result}


def _group_trades(trades, intent: dict) -> dict:
    start = float(intent.get("submit_started") or intent.get("created_ts") or 0) - 2
    limit_price = Decimal(str(intent["limit_price"]))
    groups: dict[str, dict] = {}
    for trade in trades:
        if str(trade.get("side") or "").upper() not in {"BUY", ""}:
            continue
        when = float(trade.get("matched_at") or 0)
        if when < start:
            continue
        price = Decimal(str(trade.get("price") or 0))
        size = Decimal(str(trade.get("size") or 0))
        if size <= 0 or price <= 0 or price > limit_price + Decimal("0.000001"):
            continue
        order_id = str(trade.get("taker_order_id") or trade.get("order_id") or "")
        if not order_id:
            continue
        slot = groups.setdefault(order_id, {"shares": Decimal(0), "cost_usd": Decimal(0), "order_id": order_id})
        slot["shares"] += size
        slot["cost_usd"] += size * price
    signed_shares = (Decimal(intent["signed_taker"]) / _SCALE) if intent.get("signed_taker") else None
    kept = {}
    for order_id, slot in groups.items():
        if signed_shares is not None and slot["shares"] > signed_shares + Decimal("0.05"):
            continue
        kept[order_id] = {
            "shares": slot["shares"],
            "cost_usd": slot["cost_usd"],
            "order_id": order_id,
        }
    return kept


def _trades_cost(trades, order_id) -> dict | None:
    if not trades or not order_id:
        return None
    shares = Decimal(0)
    cost = Decimal(0)
    for trade in trades:
        if str(trade.get("taker_order_id") or "") != str(order_id):
            continue
        size = Decimal(str(trade.get("size") or 0))
        price = Decimal(str(trade.get("price") or 0))
        shares += size
        cost += size * price
    if shares <= 0:
        return None
    return {"shares": shares, "cost_usd": cost, "order_id": order_id}


class PolymarketBroker:
    """Official polymarket-client adapter. Constructing it does not connect or trade."""

    def __init__(self, environ: dict):
        self.environ = environ
        self.client = None
        self.secrets = secret_values(environ)

    async def connect(self):
        if self.client is not None:
            return self.client
        import logging
        from polymarket import AsyncSecureClient, BuilderApiKey, RelayerApiKey

        class _Redact(logging.Filter):
            def filter(self, record):
                record.msg = persist.redact_text(str(record.getMessage()), secret_values(self_environ))
                record.args = ()
                return True

        self_environ = self.environ
        redact = _Redact()
        for name in ("polymarket", "polybtc.polymarket"):
            logger = logging.getLogger(name)
            logger.addFilter(redact)
            logger.setLevel(logging.WARNING)
        kwargs = {
            "private_key": self.environ["POLYMARKET_PRIVATE_KEY"],
            "wallet": self.environ["POLYMARKET_WALLET_ADDRESS"],
        }
        if self.environ.get("POLYMARKET_RELAYER_API_KEY") and self.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS"):
            kwargs["api_key"] = RelayerApiKey(
                key=self.environ["POLYMARKET_RELAYER_API_KEY"],
                address=self.environ["POLYMARKET_RELAYER_API_KEY_ADDRESS"],
            )
        elif (
            self.environ.get("POLYMARKET_BUILDER_API_KEY")
            and self.environ.get("POLYMARKET_BUILDER_SECRET")
            and self.environ.get("POLYMARKET_BUILDER_PASSPHRASE")
        ):
            kwargs["api_key"] = BuilderApiKey(
                key=self.environ["POLYMARKET_BUILDER_API_KEY"],
                secret=self.environ["POLYMARKET_BUILDER_SECRET"],
                passphrase=self.environ["POLYMARKET_BUILDER_PASSPHRASE"],
            )
        self.client = await AsyncSecureClient.create(**kwargs)
        return self.client

    async def geoblock(self) -> dict:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(trust_env=False, headers={"User-Agent": "polybtc/1.0"}) as session:
            async with session.get(gates.OFFICIAL_GEOBLOCK_URL, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"geoblock_http_{response.status}")
                payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise RuntimeError("geoblock_malformed")
        return {key: payload.get(key) for key in ("blocked", "country", "region")}

    async def closed_only(self) -> bool:
        return bool(await self.client.get_closed_only_mode())

    async def prepare_buy(self, *, asset_id: str, amount: str, max_price: str, order_type: str) -> dict:
        if order_type not in {"FAK", "FOK"}:
            raise NotSent("order_type")
        signed = await self.client.create_market_order(
            asset_id=asset_id,
            side="BUY",
            amount=amount,
            max_price=max_price,
            order_type=order_type,
        )
        return {
            "signed": signed,
            "salt": getattr(signed, "salt", None),
            "signed_maker": getattr(signed, "maker_amount", None),
            "signed_taker": getattr(signed, "taker_amount", None),
            "limit_price": max_price,
            "amount": amount,
        }

    async def post(self, prepared: dict) -> dict:
        signed = prepared["signed"]
        try:
            response = await self.client.post_order(signed)
        except Exception as exc:
            name = type(exc).__name__
            if "UserInput" in name or "Validation" in name:
                raise NotSent(name) from exc
            raise AmbiguousSubmit(name) from exc
        return normalize_sdk_response(response, prepared)

    async def get_order(self, order_id: str) -> dict | None:
        order = await self.client.get_order(order_id=order_id)
        return _open_order(order)

    async def list_open_orders(self, asset_id: str) -> list[dict]:
        page = await self.client.list_open_orders(asset_id=asset_id).first_page()
        return [_open_order(item) for item in page.items]

    async def list_trades(self, asset_id: str, after: str) -> list[dict]:
        page = await self.client.list_account_trades(asset_id=asset_id, after=after).first_page()
        out = []
        for item in page.items:
            matched = getattr(item, "matched_at", None)
            out.append({
                "taker_order_id": getattr(item, "taker_order_id", None),
                "side": getattr(item, "side", None),
                "price": getattr(item, "price", None),
                "size": getattr(item, "size", None),
                "matched_at": matched.timestamp() if hasattr(matched, "timestamp") else 0,
                "asset_id": str(getattr(item, "asset_id", asset_id)),
            })
        return out

    async def cancel_all(self) -> dict:
        result = await self.client.cancel_all()
        return {
            "canceled": [str(item) for item in getattr(result, "canceled", ())],
            "not_canceled": {str(k): str(v) for k, v in dict(getattr(result, "not_canceled", {}) or {}).items()},
        }

    async def close(self) -> None:
        if self.client is not None:
            closer = getattr(self.client, "close", None)
            if closer:
                await closer()
            self.client = None


def normalize_sdk_response(response, prepared: dict) -> dict:
    if getattr(response, "ok", False) is not True:
        return {
            "disposition": "rejected",
            "order_id": None,
            "status": "rejected",
            "error": str(getattr(response, "message", "") or getattr(response, "code", "") or "rejected")[:180],
            "fill": None,
        }
    status = str(getattr(response, "status", "") or "").lower()
    limit_price = Decimal(str(prepared.get("limit_price") or "1"))
    signed_cost = Decimal(str(prepared.get("amount") or "0"))
    fill = interpret_buy_amounts(
        getattr(response, "making_amount", 0),
        getattr(response, "taking_amount", 0),
        signed_cost,
        limit_price,
    )
    disposition = "accepted"
    if status == "matched" and (fill is None or fill.get("basis") == "zero"):
        disposition = "ambiguous"
    return {
        "disposition": disposition,
        "order_id": str(getattr(response, "order_id", "") or "") or None,
        "status": status,
        "error": None,
        "fill": fill,
    }


def _open_order(order) -> dict:
    if isinstance(order, dict):
        return order
    return {
        "id": getattr(order, "id", None),
        "asset_id": str(getattr(order, "asset_id", "")),
        "side": getattr(order, "side", None),
        "price": getattr(order, "price", None),
        "original_size": getattr(order, "original_size", None),
        "size_matched": getattr(order, "size_matched", None),
        "status": getattr(order, "status", None),
    }
