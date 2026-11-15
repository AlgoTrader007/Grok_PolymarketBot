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
    saved_intent = book._intent(intent["id"])
    assert saved_intent["reason"] == "submit_error:TimeoutError"
    assert saved_intent["error_class"] == "TimeoutError"
    assert "lost response" in saved_intent["error_message"]
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


class _Explode(FakeBroker):
    def __init__(self, secret: str = ""):
        super().__init__()
        self.mode = "explode"
        self.secret = secret
        self.secrets = [secret] if secret else []

    async def post(self, prepared):
        self.placed += 1
        if self.mode == "match":
            return {
                "disposition": "accepted", "order_id": "oid-full", "status": "matched",
                "fill": {"shares": Decimal("30"), "cost_usd": Decimal("15"), "basis": "human"},
            }
        message = "http 500"
        if self.secret:
            message = f"http 500 {self.secret}"
        raise live_exec.AmbiguousSubmit("PolyApiException", message)


class _Hang(FakeBroker):
    async def post(self, prepared):
        self.placed += 1
        await asyncio.sleep(5)


class _Validate(FakeBroker):
    async def post(self, prepared):
        self.placed += 1
        raise live_exec.NotSent(
            "validation:UserInputError", error_class="UserInputError", error_message="amount rejected",
        )


class _Balance(FakeBroker):
    def __init__(self):
        super().__init__()
        self.balance = 100.0
        self.allowance = 100.0
        self.calls = 0

    async def balance_allowance(self):
        self.calls += 1
        return {"balance": self.balance, "allowance": self.allowance}


class _FlakyLists(FakeBroker):
    def __init__(self):
        super().__init__()
        self.list_calls = 0

    async def post(self, prepared):
        self.placed += 1
        raise live_exec.AmbiguousSubmit("RequestError", "connection reset")

    async def list_open_orders(self, asset_id):
        self.list_calls += 1
        if self.list_calls == 1:
            raise RuntimeError("not indexed yet")
        return []


class _Partial1330(FakeBroker):
    async def post(self, prepared):
        self.placed += 1
        return {
            "disposition": "accepted", "order_id": "oid-1330", "status": "matched",
            "fill": {"shares": Decimal("133"), "cost_usd": Decimal("13.30"), "basis": "human"},
        }


def test_wait_for_timeout_is_not_the_same_label_as_a_post_exception(tmp_path):
    engine, book, broker = _engine(tmp_path, _Hang())
    engine.limits["submit_timeout_s"] = 0.05
    intent = book.begin_intent(SPEC, 2)
    result = asyncio.run(engine.submit(intent["id"]))
    assert result["status"] == "submit_unknown"
    assert result["reason"] == "submit_timeout"
    assert result["error_class"] == "TimeoutError"
    assert "wait_for" in result["error_message"]
    assert broker.placed == 1
    assert book.begin_intent(SPEC, 2) is None


def test_validation_reject_records_the_class_and_does_not_reserve(tmp_path):
    engine, book, broker = _engine(tmp_path, _Validate())
    intent = book.begin_intent(SPEC, 2)
    result = asyncio.run(engine.submit(intent["id"]))
    assert result["status"] == "not_sent"
    assert result["reason"] == "validation:UserInputError"
    assert result["error_class"] == "UserInputError"
    assert "amount rejected" in result["error_message"]
    assert book.reserved_cost() == 0
    assert book.positions == []
    assert broker.placed == 1


def test_ambiguous_exception_is_reconciled_absent_and_retried_once(tmp_path):
    secret = "0x" + "cd" * 32
    engine, book, broker = _engine(tmp_path, _Explode(secret))
    first = book.begin_intent(SPEC, 2)
    result = asyncio.run(engine.submit(first["id"]))
    assert result["status"] == "submit_unknown"
    assert result["reason"] == "submit_error:PolyApiException"
    assert result["error_class"] == "PolyApiException"
    assert secret not in result["error_message"]
    assert "[redacted]" in result["error_message"]
    assert secret not in (tmp_path / "execution_state.json").read_text(encoding="utf-8")
    assert book.reserved_cost() == 15
    assert book.begin_intent(SPEC, 2) is None

    resolved = asyncio.run(engine.reconcile_after_unknown(first["id"], delay_s=0, attempts=2))
    assert resolved["status"] == "absent"
    assert resolved["reason"] == "confirmed_not_placed"
    assert book.reserved_cost() == 0
    assert broker.placed == 1

    broker.mode = "match"
    second = book.begin_intent(SPEC, 2)
    assert second is not None
    filled = asyncio.run(engine.submit(second["id"]))
    assert filled["status"] == "filled"
    assert book.begin_intent(SPEC, 2) is None
    assert book.begin_intent(dict(SPEC, side="Down"), 2) is None
    assert broker.placed == 2


def test_reconcile_that_finds_a_fill_does_not_retry(tmp_path):
    engine, book, broker = _engine(tmp_path, _Explode())
    intent = book.begin_intent(SPEC, 2)
    asyncio.run(engine.submit(intent["id"]))
    broker.trades = [{
        "taker_order_id": "oid-recovered",
        "side": "BUY",
        "price": "0.50",
        "size": "30",
        "matched_at": 10**12,
        "asset_id": "123",
    }]
    resolved = asyncio.run(engine.reconcile_after_unknown(intent["id"], delay_s=0, attempts=3))
    assert resolved["status"] == "filled"
    assert len(book.positions) == 1
    assert book.positions[0]["shares"] == 30
    assert book.reserved_cost() == 0
    assert book.open_cost() == 15
    assert book.begin_intent(SPEC, 2) is None
    assert book.begin_intent(dict(SPEC, side="Down"), 2) is None
    assert broker.placed == 1


def test_failed_lookup_is_retried_then_a_miss_releases_the_stake(tmp_path):
    engine, book, broker = _engine(tmp_path, _FlakyLists())
    intent = book.begin_intent(SPEC, 2)
    asyncio.run(engine.submit(intent["id"]))
    still = asyncio.run(engine.reconcile_after_unknown(intent["id"], delay_s=0, attempts=1))
    assert still["status"] == "submit_unknown"
    assert book.reserved_cost() == 15
    resolved = asyncio.run(engine.reconcile_after_unknown(intent["id"], delay_s=0, attempts=2))
    assert resolved["status"] == "absent"
    assert book.reserved_cost() == 0
    assert broker.list_calls >= 2
    assert broker.placed == 1


def test_live_fak_partial_keeps_actual_cost_and_does_not_reserve_the_rest(tmp_path):
    engine, book, broker = _engine(tmp_path, _Partial1330())
    intent = book.begin_intent(dict(SPEC, limit_price=0.10, side="Down"), 2)
    result = asyncio.run(engine.submit(intent["id"]))
    assert result["status"] == "partial"
    assert result["shares"] == 133
    assert result["cost_usd"] == 13.3
    assert book.positions[0]["partial"] is True
    assert book.positions[0]["cost_usd"] == 13.3
    assert book.open_cost() == 13.3
    assert book.reserved_cost() == 0
    assert book.begin_intent(dict(SPEC, side="Down"), 2) is None
    assert book.begin_intent(SPEC, 2) is None
    assert broker.placed == 1


def test_balance_cache_skips_a_live_submit_when_pusd_is_short(tmp_path):
    engine, book, broker = _engine(tmp_path, _Balance())
    assert asyncio.run(engine.refresh_balance()) == 100
    broker.balance = 1
    assert asyncio.run(engine.refresh_balance()) == 100
    engine._balance_mono = 0
    assert asyncio.run(engine.refresh_balance()) == 1
    assert broker.calls == 2
    intent = book.begin_intent(SPEC, 2)
    result = asyncio.run(engine.submit(intent["id"]))
    assert result["status"] == "not_sent"
    assert result["reason"] == "insufficient_balance"
    assert broker.placed == 0
    assert broker.prepared == 0
    assert book.positions == []

    paper, _paper_book, paper_broker = _engine(tmp_path / "paper", _Balance())
    paper.limits = dict(paper.limits, requested=False, ready=False)
    paper_broker.balance = 0
    assert asyncio.run(paper.refresh_balance()) is None
    assert paper_broker.calls == 0


def test_collateral_snapshot_converts_base_units():
    class Snap:
        balance = 15_000_000
        allowances = {"0xabc": 20_000_000, "0xdef": 16_000_000}

    snap = live_exec.collateral_snapshot(Snap())
    assert snap == {"balance": 15.0, "allowance": 16.0}
    empty = live_exec.collateral_snapshot({"balance": "15000000", "allowances": {}})
    assert empty["balance"] == 15.0
    assert empty["allowance"] == 0.0


def _unknown(intent_id: str, start: int, *, end_offset: int = 300) -> dict:
    return {
        "id": intent_id,
        "slug": f"btc-updown-5m-{start}",
        "start": start,
        "end": start + end_offset,
        "side": "Down",
        "token_id": "999",
        "stake_usd": 15.0,
        "limit_price": 0.10,
        "fee_rate": 0.07,
        "fee_exp": 1,
        "status": "submit_unknown",
        "reason": "submit_timeout",
        "order_id": None,
        "shares": 0.0,
        "cost_usd": 0.0,
        "created_ts": start + 20,
    }


def _load_stuck(tmp_path, intents, positions):
    payload = {
        "version": 1,
        "intents": intents,
        "positions": positions,
        "pending_csv": [],
        "realized": 0,
        "n_trades": 0,
        "n_wins": 0,
        "kill": False,
    }
    path = tmp_path / "execution_state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    book = ExecutionBook(path, tmp_path / "KILL")
    assert book.load()
    return book


def test_paper_startup_expires_stuck_unknowns_without_placing(tmp_path):
    """Nine window-ended submit_unknown rows, the shape left in execution_state.json."""
    base = 1_763_000_000
    intents = [_unknown(f"unk-{index}", base + index * 300) for index in range(9)]
    intents.append({
        "id": "filled-intent",
        "slug": f"btc-updown-5m-{base - 300}",
        "start": base - 300,
        "end": base,
        "side": "Up",
        "token_id": "111",
        "stake_usd": 15.0,
        "status": "filled",
        "shares": 30.0,
        "cost_usd": 15.0,
        "order_id": "oid-filled",
    })
    positions = [{
        "intent_id": "filled-intent",
        "mode": "live",
        "slug": f"btc-updown-5m-{base - 300}",
        "start": base - 300,
        "end": base,
        "side": "Up",
        "shares": 30.0,
        "cost_usd": 15.0,
    }]
    book = _load_stuck(tmp_path, intents, positions)
    assert book.reserved_cost() == 135
    now = intents[8]["end"] + 7200
    engine = Engine(book, None, {"unknown_expire_s": 3600, "requested": False, "ready": False})
    released = asyncio.run(engine.startup_cleanup(now, cancel_resting=False))
    assert engine.broker is None
    assert len(released) == 9
    assert book.reserved_cost() == 0
    assert book.open_cost() == 15
    expired = [item for item in book.intents if item["id"].startswith("unk-")]
    assert [item["status"] for item in expired] == ["expired"] * 9
    assert {item["reason"] for item in expired} == {"unresolved_after_window"}
    assert book._intent("filled-intent")["status"] == "filled"
    fresh = _unknown("still-open", now - 30, end_offset=300)
    book.intents.append(fresh)
    book.save()
    again = asyncio.run(engine.startup_cleanup(now, cancel_resting=False))
    assert book._intent("still-open")["status"] == "submit_unknown"
    assert all(item["id"] != "still-open" for item in again)
    assert book.reserved_cost() == 15


def test_startup_reconcile_marks_a_confirmed_miss_absent(tmp_path):
    base = 1_763_000_000
    intents = [_unknown(f"unk-{index}", base + index * 300) for index in range(9)]
    book = _load_stuck(tmp_path, intents, [])
    broker = FakeBroker()
    engine = Engine(book, broker, {"unknown_expire_s": 3600, "requested": True, "ready": True})
    now = intents[-1]["end"] + 7200
    released = asyncio.run(engine.startup_cleanup(now, cancel_resting=False))
    assert len(released) == 9
    assert {item["status"] for item in released} == {"absent"}
    assert {item["reason"] for item in released} == {"confirmed_not_placed"}
    assert book.reserved_cost() == 0
    assert broker.placed == 0
    assert broker.cancels == 0
    assert broker.prepared == 0


def test_window_end_reconcile_releases_a_miss_and_expiry_covers_a_failed_lookup(tmp_path):
    engine, book, broker = _engine(tmp_path)
    intent = book.begin_intent(SPEC, 2)
    book.mark(intent["id"], status="submit_unknown", reason="submit_error:PolyApiException", shares=0)
    assert book.reserved_cost() == 15
    released = asyncio.run(engine.sweep_ended(SPEC["end"] + 30))
    assert book._intent(intent["id"])["status"] == "absent"
    assert released[0]["reason"] == "confirmed_not_placed"
    assert book.reserved_cost() == 0
    assert broker.placed == 0

    held, held_book, held_broker = _engine(tmp_path / "held")
    stuck = held_book.begin_intent(SPEC, 2)
    held_book.mark(stuck["id"], status="submit_unknown", shares=0)
    held_broker.fail_lists = True
    still = asyncio.run(held.sweep_ended(SPEC["end"] + 30))
    assert still == []
    assert held_book._intent(stuck["id"])["status"] == "submit_unknown"
    assert held_book.reserved_cost() == 15
    expired = asyncio.run(held.sweep_ended(SPEC["end"] + 4000))
    assert held_book._intent(stuck["id"])["status"] == "expired"
    assert held_book._intent(stuck["id"])["reason"] == "unresolved_after_window"
    assert expired[0]["id"] == stuck["id"]
    assert held_book.reserved_cost() == 0
    assert held_broker.placed == 0


def test_a_resting_order_is_not_dropped_just_because_the_window_ended(tmp_path):
    engine, book, broker = _engine(tmp_path)
    intent = book.begin_intent(SPEC, 2)
    book.mark(intent["id"], status="submit_unknown", order_id="oid-live", shares=0)
    broker.orders["oid-live"] = {
        "id": "oid-live", "asset_id": "123", "side": "BUY", "price": "0.50",
        "original_size": "30", "size_matched": "0", "status": "LIVE",
    }
    asyncio.run(engine.sweep_ended(SPEC["end"] + 4000))
    saved = book._intent(intent["id"])
    assert saved["status"] == "submit_unknown"
    assert saved["reason"] == "resting_cancel_requested"
    assert book.reserved_cost() == 15
    assert broker.cancels == 1
    assert broker.placed == 0
    broker.orders.clear()
    released = asyncio.run(engine.sweep_ended(SPEC["end"] + 4000))
    assert book._intent(intent["id"])["status"] == "absent"
    assert book.reserved_cost() == 0
    assert released[0]["reason"] == "confirmed_not_placed"
