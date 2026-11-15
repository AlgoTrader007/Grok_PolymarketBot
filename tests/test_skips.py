"""Skip rows, the read-only skip report, and start-event fields."""
import json
from pathlib import Path

import model
import skip_report


def test_skip_report_counts_reasons_by_day_and_window(tmp_path):
    path = tmp_path / "skips.jsonl"
    rows = [
        {
            "ts_local": "2026-11-13T01:14:00.000-05:00",
            "slug": "btc-updown-5m-1763000000",
            "side": "Down",
            "reasons": ["exposure_cap", "already_entered"],
        },
        {
            "ts_local": "2026-11-13T02:22:00.000-05:00",
            "slug": "btc-updown-5m-1763000300",
            "side": "Up",
            "reasons": ["insufficient_balance", "too_late"],
        },
        {
            "ts_local": "2026-11-14T00:01:00.000-05:00",
            "slug": "btc-updown-5m-1763086400",
            "side": "Down",
            "reasons": ["live_not_allowed"],
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    day = skip_report.report_text(path, day="2026-11-13", since=None)
    assert "rows: 2" in day
    assert "exposure_cap: 1" in day
    assert "already_entered: 1" in day
    assert "insufficient_balance: 1" in day
    assert "too_late: 1" in day
    assert "live_not_allowed" not in day
    assert "btc-updown-5m-1763000000" in day
    assert "Down: already_entered 1, exposure_cap 1" in day
    assert "orders: not sent" in day
    later = skip_report.report_text(path, day=None, since="2026-11-14")
    assert "rows: 1" in later
    assert "live_not_allowed: 1" in later
    both = skip_report.report_text(path, day="2026-11-13", since="2026-11-14")
    assert "rows: 0" in both
    missing = skip_report.report_text(tmp_path / "absent.jsonl", day="2026-11-13", since=None)
    assert "rows: 0" in missing
    assert "orders: not sent" in missing


def test_note_skip_logs_each_reason_change_once_per_window_side(monkeypatch):
    import bot

    old = bot.S
    bot.S = bot.State()
    try:
        assert bot.S.note_skip(100, "Up", ["too_early", "exposure_cap"]) is True
        assert bot.S.note_skip(100, "Up", ["too_early", "exposure_cap"]) is False
        assert bot.S.note_skip(100, "Down", ["too_early", "exposure_cap"]) is True
        assert bot.S.note_skip(100, "Up", ["too_late"]) is True
        assert bot.S.note_skip(100, "Up", []) is False
        assert bot.S.note_skip(100, "Up", ["too_late"]) is True
        row = bot.skip_record(
            slug="btc-updown-5m-100", start=100, side="Up", mode="live_blocked",
            reasons=["exposure_cap", "live_not_allowed", "already_entered", "constraints_unknown", "too_early", "too_late", "insufficient_balance"],
            fair=0.62, edge=0.01, ask=0.55, open_cost=15, reserved_cost=135, cap=75,
        )
        assert row["reasons"] == [
            "exposure_cap", "live_not_allowed", "already_entered", "constraints_unknown",
            "too_early", "too_late", "insufficient_balance",
        ]
        assert row["ts_local"]
        assert row["open_cost"] == 15 and row["reserved_cost"] == 135 and row["cap"] == 75
        assert bot.entry_ready({"action": "enter", "cost_usd": 13.3, "shares": 133}, 15) is False
        assert bot.entry_ready({"action": "enter", "cost_usd": 15, "shares": 30}, 15) is True
        assert bot.should_open_readonly_reconcile({"ready": False}, {}) is False
        assert bot.should_open_readonly_reconcile(
            {"ready": False},
            {"POLYMARKET_PRIVATE_KEY": "k" * 16, "POLYMARKET_WALLET_ADDRESS": "0x" + "11" * 20},
        ) is True
        assert bot.should_open_readonly_reconcile(
            {"ready": False},
            {
                "POLYMARKET_PRIVATE_KEY": "k" * 16,
                "POLYMARKET_WALLET_ADDRESS": "0x" + "11" * 20,
                "HTTPS_PROXY": "http://127.0.0.1:9",
            },
        ) is False
        assert bot.should_open_readonly_reconcile({"ready": True}, {"POLYMARKET_PRIVATE_KEY": "k" * 16, "POLYMARKET_WALLET_ADDRESS": "w" * 16}) is False
    finally:
        bot.S = old


def test_trader_eval_appends_a_skip_row_when_the_reason_set_changes(monkeypatch, tmp_path):
    import bot
    from live_exec import Engine, ExecutionBook

    now = 1_700_000_130.5
    cur = int(now) - int(now) % 300
    rows = []
    monkeypatch.setattr(bot.time, "time", lambda: now)
    monkeypatch.setattr(bot, "jlog", lambda name, rec: rows.append((name, rec)))
    old = bot.S
    bot.S = bot.State()
    try:
        book = ExecutionBook(tmp_path / "execution_state.json", tmp_path / "KILL")
        book.positions.append({
            "start": cur - 300, "cost_usd": 70, "shares": 10, "side": "Up", "slug": "other",
        })
        bot.S.book = book
        bot.S.mode = "live_blocked"
        bot.S.limits = {
            "requested": True, "ready": True, "max_attempts": 2, "max_open_cost_usd": 75, "kill_env": False,
        }
        bot.S.engine = Engine(book, None, bot.S.limits)
        bot.S.engine.entry_allowed = False
        bot.S.engine.balance_usd = 1.0
        bot.S.engine.allowance_usd = 1.0
        bot.S.sigma = model.ann_to_sec(0.5)
        bot.S.spot = {sec: 100_000.0 for sec in range(cur, int(now) + 1)}
        bot.S.spot_recv = {sec: sec + 0.1 for sec in bot.S.spot}
        bot.S.twap = {cur: 100_000.0}
        bot.S.twap_recv = {cur: cur + 0.2}
        bot.S.markets[cur] = {
            "start": cur, "end": cur + 300, "slug": f"btc-updown-5m-{cur}",
            "up": "up-token", "down": "down-token",
            "fee_rate": 0.07, "fee_exp": 1, "accepting": True,
            "tick": "0.01", "min_order_size": 5, "version": "v1",
            "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "crypto_config": {"asset": "btc", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": 60},
            "ptb": 100_000.0, "ptb_fresh": True, "ptb_locked": True,
        }
        level = {"asks": {0.50: 100.0}, "bids": {0.49: 10.0}, "t": now, "exch_ts": now, "tick": 0.01, "min_order_size": 5}
        bot.S.books = {"up-token": dict(level), "down-token": dict(level)}
        bot.trader_eval(force=True)
        skips = [rec for name, rec in rows if name == "skips.jsonl"]
        assert len(skips) == 2
        assert bot.S.pending_submits == []
        for rec in skips:
            assert "exposure_cap" in rec["reasons"]
            assert "live_not_allowed" in rec["reasons"]
            assert "insufficient_balance" in rec["reasons"]
            assert "edge" in rec["reasons"]
            assert rec["open_cost"] == 70
            assert rec["reserved_cost"] == 0
            assert rec["cap"] == 75
            assert rec["ts_local"]
            assert rec["mode"] == "live_blocked"
        assert any(name == "signals.jsonl" and "exposure_cap" in rec["blocked"] for name, rec in rows)
        bot.trader_eval(force=True)
        assert len([rec for name, rec in rows if name == "skips.jsonl"]) == 2
        book.positions.append({
            "start": cur, "cost_usd": 15, "shares": 30, "side": "Up", "slug": f"btc-updown-5m-{cur}",
        })
        bot.trader_eval(force=True)
        again = [rec for name, rec in rows if name == "skips.jsonl"]
        assert len(again) == 4
        by_side = {rec["side"]: rec["reasons"] for rec in again[-2:]}
        assert "already_entered" in by_side["Up"]
        assert "opposite_side" in by_side["Down"]
        assert bot.S.pending_submits == []
    finally:
        bot.S = old


def test_start_and_submit_records_carry_the_new_fields(tmp_path):
    import bot

    old = bot.S
    bot.S = bot.State()
    try:
        bot.S.mode = "paper"
        bot.S.live_reason = "live_disabled"
        bot.S.limits = {
            "max_open_cost_usd": 75, "order_type": "FAK", "submit_timeout_s": 20, "max_attempts": 2,
        }
        event = bot.start_event()
    finally:
        bot.S = old
    for key in ("max_open_cost_usd", "order_type", "submit_timeout_s", "stake", "max_attempts_per_window", "open_cost", "reserved_cost"):
        assert key in event
    assert event["order_type"] == "FAK"
    assert event["stake"] == 15
    assert event["max_attempts_per_window"] == 2
    record = bot.live_submit_record("abc", {
        "status": "submit_unknown", "reason": "submit_error:PolyApiException",
        "order_id": None, "shares": 0, "cost_usd": 0, "cost_basis": None,
        "error_class": "PolyApiException", "error_message": "http 500",
    })
    assert record["type"] == "live_submit"
    assert record["error_class"] == "PolyApiException"
    assert record["error_message"] == "http 500"
    assert record["reason"] == "submit_error:PolyApiException"
    assert Path(tmp_path).exists()
