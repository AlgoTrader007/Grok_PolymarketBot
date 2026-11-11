"""Mocked order adapter: auth, partial fills, timeouts, restarts, and redaction."""
import asyncio
import json
from decimal import Decimal

import live_exec
from live_exec import ExecutionBook, Engine


LIMITS = {
    "ready": True,
    "requested": True,
    "reasons": [],
    "order_type": "FAK",
    "max_open_cost_usd": 75,
    "max_attempts": 2,
    "submit_timeout_s": 5,
    "geoblock_max_age_s": 30,
    "kill_env": False,
}
SPEC = {
    "slug": "btc-updown-5m-1700000000",
    "start": 1700000000,
    "end": 1700000300,
    "side": "Up",
    "token_id": "123",
    "stake_usd": 15,
    "limit_price": 0.50,
    "fee_rate": 0.07,
    "fee_exp": 1,
    "fair_prob": 0.6,
    "edge": 0.08,
    "entry_time_la": "2026-11-10T12:00:00.000-08:00",
}


class FakeBroker:
    def __init__(self):
        self.placed = 0
        self.prepared = 0
        self.mode = "match"
        self.client = object()
        self.geo = {"blocked": False, "country": "NL", "region": "NH"}
        self.closed = False
        self.orders = {}
        self.trades = []
        self.fail_lists = False
        self.cancels = 0
        self.last_prepare = None

    async def geoblock(self):
        if self.geo is None:
            raise RuntimeError("down")
        return dict(self.geo)

    async def closed_only(self):
        return self.closed

    async def prepare_buy(self, **kwargs):
        self.prepared += 1
        self.last_prepare = kwargs
        return {
            "signed": {"signature": "0xshould-never-be-stored"},
            "salt": 7,
            "signed_maker": 15_000_000,
            "signed_taker": 30_000_000,
            "limit_price": kwargs["max_price"],
            "amount": kwargs["amount"],
        }

    async def post(self, prepared):
        self.placed += 1
        if self.mode == "timeout":
            raise TimeoutError("lost response")
        if self.mode == "reject":
            return {"disposition": "rejected", "order_id": None, "status": "rejected", "error": "fak_not_filled", "fill": None}
        if self.mode == "partial":
            return {
                "disposition": "accepted", "order_id": "oid-partial", "status": "matched",
                "fill": {"shares": Decimal("10"), "cost_usd": Decimal("5"), "basis": "human"},
            }
        if self.mode == "ambiguous":
            return {"disposition": "ambiguous", "order_id": None, "status": "matched", "fill": None}
        return {
            "disposition": "accepted", "order_id": "oid-full", "status": "matched",
            "fill": {"shares": Decimal("30"), "cost_usd": Decimal("15"), "basis": "human"},
        }

    async def get_order(self, order_id):
        return self.orders.get(order_id)

    async def list_open_orders(self, asset_id):
        if self.fail_lists:
            raise RuntimeError("down")
        return [order for order in self.orders.values() if order.get("asset_id") == asset_id]

    async def list_trades(self, asset_id, after):
        if self.fail_lists:
            raise RuntimeError("down")
        return list(self.trades)

    async def cancel_all(self):
        self.cancels += 1
        return {"canceled": ["oid"], "not_canceled": {}}


def _engine(tmp_path, broker=None):
    book = ExecutionBook(tmp_path / "execution_state.json", tmp_path / "KILL")
    broker = broker or FakeBroker()
    return Engine(book, broker, dict(LIMITS)), book, broker


def test_live_config_stays_off_without_an_explicit_local_enable(tmp_path):
    cfg = {"live": {"enabled": False}}
    off = live_exec.live_config(cfg, {})
    assert off["ready"] is False
    assert off["requested"] is False
    enabled = live_exec.live_config({"live": {"enabled": True}}, {"POLYBTC_LIVE_CONFIRM": "YES"})
    assert enabled["requested"] is True
    assert enabled["ready"] is False
    assert "private_key_missing" in enabled["reasons"]
    assert "wallet_missing" in enabled["reasons"]
    proxied = live_exec.live_config(
        {"live": {"enabled": True}},
        {
            "POLYBTC_LIVE_CONFIRM": "YES",
            "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
            "POLYMARKET_WALLET_ADDRESS": "0x" + "11" * 20,
            "HTTPS_PROXY": "http://127.0.0.1:9",
        },
    )
    assert proxied["ready"] is False
    assert "proxy_env" in proxied["reasons"]
    ready = live_exec.live_config(
        {"live": {"enabled": True}},
        {
            "POLYBTC_LIVE_CONFIRM": "YES",
            "POLYMARKET_PRIVATE_KEY": "0x" + "ab" * 32,
            "POLYMARKET_WALLET_ADDRESS": "0x" + "11" * 20,
        },
    )
    assert ready["ready"] is True
    assert "YES" != "yes"
    assert live_exec.live_config({"live": {"enabled": True}}, {"POLYBTC_LIVE_CONFIRM": "yes"})["requested"] is False


def test_rejected_partial_and_geoblock_do_not_invent_a_full_fill(tmp_path):
    engine, book, broker = _engine(tmp_path)
    broker.mode = "reject"
    intent = book.begin_intent(SPEC, 2)
    asyncio.run(engine.submit(intent["id"]))
    assert broker.placed == 1
    assert book.positions == []
    assert book._intent(intent["id"])["status"] == "rejected"
    again = book.begin_intent(SPEC, 2)
    assert again is not None
    broker.mode = "partial"
    asyncio.run(engine.submit(again["id"]))
    assert len(book.positions) == 1
    assert book.positions[0]["shares"] == 10
    assert book.positions[0]["cost_usd"] == 5
    assert book.positions[0]["partial"] is True
    assert book.begin_intent(dict(SPEC, side="Down"), 2) is None
    assert book.begin_intent(SPEC, 2) is None

    blocked, blocked_book, blocked_broker = _engine(tmp_path / "geo")
    blocked_broker.geo = {"blocked": True, "country": "US", "region": "VA"}
    ident = blocked_book.begin_intent(SPEC, 2)
    asyncio.run(blocked.submit(ident["id"]))
    assert blocked_broker.placed == 0
    assert blocked_book._intent(ident["id"])["status"] == "not_sent"
    assert blocked_book.begin_intent(SPEC, 2) is None

    united, united_book, united_broker = _engine(tmp_path / "us")
    united_broker.geo = {"blocked": False, "country": "US", "region": "VA"}
    ident = united_book.begin_intent(SPEC, 2)
    asyncio.run(united.submit(ident["id"]))
    assert united_broker.placed == 0
    assert united.block_reason == "geoblock_us"

    closed, closed_book, closed_broker = _engine(tmp_path / "closed")
    closed_broker.closed = True
    ident = closed_book.begin_intent(SPEC, 2)
    asyncio.run(closed.submit(ident["id"]))
    assert closed_broker.placed == 0
    assert closed.block_reason == "closed_only"


def test_timeout_is_reconciled_before_any_retry_and_survives_restart(tmp_path):
    engine, book, broker = _engine(tmp_path)
    broker.mode = "timeout"
    intent = book.begin_intent(SPEC, 2)
    asyncio.run(engine.submit(intent["id"]))
    assert book._intent(intent["id"])["status"] == "submit_unknown"
    assert book.begin_intent(SPEC, 2) is None
    saved = json.loads((tmp_path / "execution_state.json").read_text(encoding="utf-8"))
    assert "0xshould-never-be-stored" not in json.dumps(saved)
    assert "signature" not in json.dumps(saved)

    broker.fail_lists = True
    asyncio.run(engine.reconcile_open())
    assert book._intent(intent["id"])["status"] == "submit_unknown"
    assert broker.placed == 1

    restarted = ExecutionBook(tmp_path / "execution_state.json", tmp_path / "KILL")
    assert restarted.load()
    other = FakeBroker()
    other.trades = [{
        "taker_order_id": "oid-recovered",
        "side": "BUY",
        "price": "0.50",
        "size": "30",
        "matched_at": 10**12,
        "asset_id": "123",
    }]
    follow = Engine(restarted, other, dict(LIMITS))
    asyncio.run(follow.reconcile_open())
    assert other.placed == 0
    assert len(restarted.positions) == 1
    assert restarted.positions[0]["shares"] == 30
    assert restarted.positions[0]["order_id"] == "oid-recovered"
    assert restarted.begin_intent(SPEC, 2) is None
    assert restarted.begin_intent(dict(SPEC, side="Down"), 2) is None


def test_confirmed_absent_timeout_allows_one_retry_only(tmp_path):
    engine, book, broker = _engine(tmp_path)
    broker.mode = "timeout"
    first = book.begin_intent(SPEC, 2)
    asyncio.run(engine.submit(first["id"]))
    asyncio.run(engine.reconcile_open())
    assert book._intent(first["id"])["status"] == "absent"
    broker.mode = "reject"
    second = book.begin_intent(SPEC, 2)
    assert second is not None
    asyncio.run(engine.submit(second["id"]))
    assert book.begin_intent(SPEC, 2) is None
    assert broker.placed == 2


def test_amount_scale_and_sdk_response_normalization():
    raw = live_exec.interpret_buy_amounts(15_000_000, 30_000_000, Decimal("15"), Decimal("0.50"))
    human = live_exec.interpret_buy_amounts("15", "30", Decimal("15"), Decimal("0.50"))
    assert raw["basis"] == "e6" and raw["shares"] == Decimal("30") and raw["cost_usd"] == Decimal("15")
    assert human["basis"] == "human" and human["cost_usd"] == Decimal("15")

    class Accepted:
        ok = True
        status = "matched"
        order_id = "abc"
        making_amount = Decimal("15000000")
        taking_amount = Decimal("30000000")

    class Delayed:
        ok = True
        status = "delayed"
        order_id = "abc"
        making_amount = Decimal(0)
        taking_amount = Decimal(0)

    class Rejected:
        ok = False
        status = ""
        message = "no orders found to match with FAK order"
        code = "fak_not_filled"

    prepared = {"limit_price": "0.50", "amount": "15"}
    filled = live_exec.normalize_sdk_response(Accepted(), prepared)
    assert filled["fill"]["basis"] == "e6"
    delayed = live_exec.normalize_sdk_response(Delayed(), prepared)
    assert delayed["status"] == "delayed"
    assert live_exec.normalize_sdk_response(Rejected(), prepared)["disposition"] == "rejected"


def test_secrets_are_redacted_and_not_required_in_the_repo():
    secret = "0x" + "ab" * 32
    text = live_exec.persist.redact_text(f"failed with {secret}", [secret])
    assert secret not in text
    assert "[redacted]" in text
    names = live_exec.LIVE_ENV_NAMES
    assert "POLYMARKET_PRIVATE_KEY" in names
    assert "POLYBTC_LIVE_CONFIRM" in names
