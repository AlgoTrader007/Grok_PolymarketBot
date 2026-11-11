"""Official settlement retries. Unknown and feed-only rows are not losses."""
import json
from pathlib import Path

import settlement
from live_exec import ExecutionBook


def test_official_outcome_and_no_automatic_loss():
    assert settlement.official_outcome(True, '["1", "0"]', {"finalPrice": 101, "priceToBeat": 100}) == ("Up", "gamma_official")
    assert settlement.official_outcome(True, ["0", "1"], None) == ("Down", "gamma_outcome_prices")
    assert settlement.official_outcome(False, None, {"finalPrice": 90, "priceToBeat": 100}) == ("Down", "gamma_final_price")
    outcome, reason = settlement.official_outcome(True, ["1", "0"], {"finalPrice": 90, "priceToBeat": 100})
    assert outcome is None and reason == "official_conflict"
    assert settlement.official_outcome(False, ["0.5", "0.5"], {})[0] is None
    assert settlement.settlement_pnl({"side": "Up", "shares": 10, "cost_usd": 5, "fee_usd": 0.175}, None) is None
    won = settlement.settlement_pnl({"side": "Up", "shares": 10, "cost_usd": 5, "fee_usd": 0.175}, "Up")
    assert won["result"] == "WIN" and won["pnl_usd"] == 4.825


def test_restore_keeps_old_open_positions_and_ignores_unknown_losses(tmp_path):
    log = Path(tmp_path)
    old_end = 1_600_000_000
    (log / "trades.jsonl").write_text("\n".join([
        json.dumps({"type": "paper_entry", "slug": "btc-updown-5m-1600000000", "side": "Up", "entry_ts": 1,
                    "start": 1600000000, "end": old_end, "shares": 30, "cost_usd": 15, "fee_usd": 0.5,
                    "entry_time_la": "2026-01-01T00:00:00.000-08:00"}),
        json.dumps({"type": "paper_settle", "slug": "btc-updown-5m-1600000000", "side": "Down", "entry_ts": 2,
                    "start": 1600000300, "end": 1600000600, "shares": 30, "cost_usd": 15, "fee_usd": 0.5,
                    "result": "LOSS", "pnl_usd": -15.5, "settle_source": "rtds_twap60_feed",
                    "entry_time_la": "2026-01-01T00:05:00.000-08:00"}),
        json.dumps({"type": "paper_entry", "slug": "btc-updown-5m-1600000300", "side": "Down", "entry_ts": 2,
                    "start": 1600000300, "end": 1600000600, "shares": 30, "cost_usd": 15, "fee_usd": 0.5,
                    "entry_time_la": "2026-01-01T00:05:00.000-08:00"}),
    ]) + "\n", encoding="utf-8")
    (log / "trades.csv").write_text(
        "entry_time_la,settle_time_la,slug,side,ask_vwap,best_ask,fee_usd,fee_per_share,fair_prob,edge,shares,cost_usd,price_to_beat,final_price,result,pnl_usd,settle_source,secs_left_at_entry,sigma_ann,partial\n"
        "2026-01-01T00:10:00.000-08:00,2026-01-01T00:20:00.000-08:00,btc-updown-5m-1600000600,Up,0.5,0.5,0.5,0.01,0.6,0.08,30,15,1,2,UNKNOWN,-15.5,rtds_twap60_feed,20,0.5,False\n"
        "2026-01-02T00:00:00.000-08:00,2026-01-02T00:10:00.000-08:00,btc-updown-5m-1600000900,Up,0.5,0.5,0.5,0.01,0.6,0.08,30,15,1,2,WIN,14.5,gamma_official,20,0.5,False\n",
        encoding="utf-8",
    )
    restored = settlement.restore_from_logs(log)
    assert restored["realized"] == 14.5
    assert restored["n_trades"] == 1
    assert restored["n_wins"] == 1
    slugs = {item["slug"] for item in restored["positions"]}
    assert "btc-updown-5m-1600000000" in slugs
    assert "btc-updown-5m-1600000300" in slugs
    assert "btc-updown-5m-1600000600" in slugs
    assert "btc-updown-5m-1600000900" not in slugs
    assert all(item["end"] < 1_700_000_000 for item in restored["positions"])


def test_official_retry_writes_pnl_once_and_keeps_conflict_open(tmp_path):
    book = ExecutionBook(tmp_path / "execution_state.json")
    book.record_paper({
        "slug": "btc-updown-5m-1700000000", "start": 1700000000, "end": 1700000300, "side": "Up",
        "shares": 10, "cost_usd": 5, "fee_usd": 0.175, "entry_ts": 9, "entry_time_la": "t",
        "ask_vwap": 0.5, "partial": True,
    })
    assert book.settle_official(book.positions[0], None, None, "unresolved", "now") is None
    assert len(book.positions) == 1
    row = book.settle_official(book.positions[0], "Up", 101, "gamma_official", "now")
    assert row["result"] == "WIN"
    assert book.positions == []
    assert book.realized == 4.825
    written = book.flush_csv(tmp_path / "trades.csv")
    assert written == 1
    assert book.flush_csv(tmp_path / "trades.csv") == 0
    reloaded = ExecutionBook(tmp_path / "execution_state.json")
    assert reloaded.load()
    assert reloaded.realized == 4.825
    assert reloaded.positions == []
