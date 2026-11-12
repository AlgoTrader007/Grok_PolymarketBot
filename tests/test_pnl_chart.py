"""PnL math, official CSV rows, stale reads, and a headless PNG."""
import json
import threading
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")

import pnl_chart
from pnl_chart import Book, Trade, build_summary, cumulative, current_streak, drawdowns, filter_trades, parse_trades, unrealized_from_state

LA = ZoneInfo("America/Los_Angeles")
NY = ZoneInfo("America/New_York")
HEADER = (
    "entry_time_la,settle_time_la,slug,side,ask_vwap,best_ask,fee_usd,fee_per_share,fair_prob,edge,"
    "shares,cost_usd,price_to_beat,final_price,result,pnl_usd,settle_source,secs_left_at_entry,sigma_ann,partial\n"
)


def _row(entry, settle, result, pnl, source="gamma_official", slug="btc-updown-5m-1", side="Up", cost="15", fee="0.40"):
    return (
        f"{entry},{settle},{slug},{side},0.5,0.5,{fee},0.02,0.6,0.08,30,{cost},1,2,"
        f"{result},{pnl},{source},20,0.5,False\n"
    )


def _trade(iso, pnl, result, cost=15.0, fee=0.4):
    return Trade(datetime.fromisoformat(iso), pnl, fee, cost, result, "btc-updown-5m-1", "Up")


def test_cumulative_drawdown_and_streak():
    curve = cumulative([4.5, -8.2, 6.0])
    assert curve == [4.5, -3.7, 2.3] or [round(value, 10) for value in curve] == [4.5, -3.7, 2.3]
    downs = drawdowns([4.5, -3.7, 2.3])
    assert [round(value, 10) for value in downs] == [0.0, 8.2, 2.2]
    assert round(max(drawdowns(curve)), 10) == 8.2
    assert current_streak(["WIN", "WIN", "LOSS", "LOSS"]) == (2, "LOSS")
    assert current_streak([]) == (0, "")


def test_csv_keeps_official_rows_and_replaces_duplicates():
    text = HEADER + _row(
        "2026-11-10T10:00:00-08:00", "2026-11-10T10:05:00-08:00", "UNKNOWN", "-15", "rtds_twap60_feed",
    ) + _row(
        "2026-11-10T10:10:00-08:00", "2026-11-10T10:15:00-08:00", "LOSS", "-9", "rtds_twap60_feed",
    ) + _row(
        "2026-11-10T11:00:00-08:00", "2026-11-10T11:05:00-08:00", "WIN", "4",
    ) + _row(
        "2026-11-10T11:00:00-08:00", "2026-11-10T11:06:00-08:00", "WIN", "5",
    )
    text = text.replace("\n", "\r\n")
    trades = parse_trades(text, LA)
    assert len(trades) == 1
    assert trades[0].pnl == 5
    assert trades[0].result == "WIN"


def test_day_filter_uses_the_config_timezone():
    late = _trade("2026-11-11T22:30:00-08:00", 3, "WIN")
    morning = _trade("2026-11-11T09:00:00-08:00", -2, "LOSS")
    assert [trade.pnl for trade in filter_trades([morning, late], LA, day=datetime(2026, 11, 11).date())] == [-2, 3]
    assert [trade.pnl for trade in filter_trades([morning, late], NY, day=datetime(2026, 11, 12).date())] == [3]
    assert [trade.pnl for trade in filter_trades([morning, late], LA, since=datetime(2026, 11, 11).date())] == [-2, 3]


def test_summary_and_unrealized_marks():
    trades = [
        _trade("2026-11-10T10:00:00-08:00", 4.5, "WIN", cost=15, fee=0.4),
        _trade("2026-11-10T10:05:00-08:00", -8.2, "LOSS", cost=15, fee=0.3),
        _trade("2026-11-10T10:10:00-08:00", 6.0, "WIN", cost=15, fee=0.5),
    ]
    summary = build_summary(Book(trades=trades, state_mtime=1_000), LA, "America/Los_Angeles", now=1_001)
    assert round(summary.net, 10) == 2.3
    assert summary.fees == 1.2
    assert summary.stake == 45
    assert summary.wins == 2 and summary.losses == 1
    assert summary.win_rate == 2 / 3
    assert abs(summary.roi - (2.3 / 45)) < 1e-12
    assert summary.max_drawdown == 8.2
    assert (summary.streak_count, summary.streak_result) == (1, "WIN")
    assert summary.stale is False
    state = {
        "eval": {"slug": "btc-updown-5m-1", "sides": {"Up": {"bid": 0.6}, "Down": {"bid": 0.4}}},
        "open_positions": [
            {"slug": "btc-updown-5m-1", "side": "Up", "shares": 10, "cost_usd": 4, "fee_usd": 0.2},
            {"slug": "btc-updown-5m-9", "side": "Down", "shares": 10, "cost_usd": 5, "fee_usd": 0.2},
        ],
        "updated_la": "2026-11-10T10:10:00-08:00",
    }
    unrealized, marked, unmarked = unrealized_from_state(state)
    assert marked == 1 and unmarked == 1
    assert abs(unrealized - (10 * 0.6 - 4 - 0.2)) < 1e-12
    stale = build_summary(Book(trades=trades, state=state, state_mtime=1_000), LA, "America/Los_Angeles", now=1_020)
    assert stale.stale is True
    assert "age" in stale.stale_reason


def test_locked_read_retries_and_keeps_last_good(tmp_path, monkeypatch):
    csv_path = tmp_path / "trades.csv"
    state_path = tmp_path / "state.json"
    csv_path.write_text(HEADER + _row(
        "2026-11-10T11:00:00-08:00", "2026-11-10T11:05:00-08:00", "WIN", "4",
    ), encoding="utf-8")
    state_path.write_text(json.dumps({"updated_la": "2026-11-10T11:05:00-08:00", "open_positions": []}), encoding="utf-8")
    book = Book()
    book.refresh(csv_path, state_path, LA, sleep=lambda _seconds: None)
    assert len(book.trades) == 1

    def locked(path, retries=6, sleep=None):
        raise PermissionError("busy")

    monkeypatch.setattr(pnl_chart, "read_bytes", locked)
    book.refresh(csv_path, state_path, LA, sleep=lambda _seconds: None)
    assert book.using_last_good is True
    assert len(book.trades) == 1
    monkeypatch.undo()
    calls = {"n": 0}
    real = pnl_chart.Path.read_bytes

    def flaky(self):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        return real(self)

    monkeypatch.setattr(pnl_chart.Path, "read_bytes", flaky)
    assert pnl_chart.read_bytes(csv_path, retries=4, sleep=lambda _seconds: None).startswith(b"entry")
    assert calls["n"] == 3


def test_png_and_local_page_do_not_trade(tmp_path):
    trades = [
        _trade("2026-11-10T10:00:00-08:00", 4.5, "WIN"),
        _trade("2026-11-10T10:05:00-08:00", -8.2, "LOSS"),
        _trade("2026-11-11T09:00:00-08:00", 3.0, "WIN", cost=12, fee=0.2),
    ]
    summary = build_summary(Book(trades=trades, state_mtime=time_now()), LA, "America/Los_Angeles", now=time_now())
    png = pnl_chart.render_png(summary, LA)
    assert png.startswith(b"\x89PNG")
    out = tmp_path / "pnl.png"
    pnl_chart.save_png(summary, LA, out)
    assert out.read_bytes().startswith(b"\x89PNG")
    server = pnl_chart.serve(lambda: summary, LA, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/pnl.png", timeout=5) as response:
            body = response.read()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            page = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert body.startswith(b"\x89PNG")
    assert "does not send orders" in page
    source = (pnl_chart.ROOT / "pnl_chart.py").read_text(encoding="utf-8")
    assert "load_env_file" not in source
    assert "os.environ" not in source


def time_now():
    return 1_700_000_000.0
