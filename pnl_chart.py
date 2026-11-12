#!/usr/bin/env python3
"""Read-only live PnL chart for the paper bot.

Reads logs/trades.csv and logs/state.json beside this script. It does not
import the trading loop, read .env, or send orders. Ctrl+C closes the chart
only. Times use the timezone in config.toml.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import settlement
import view

ROOT = Path(__file__).resolve().parent
STALE_AFTER_S = view.STALE_AFTER_S


@dataclass
class Trade:
    settle: datetime
    pnl: float
    fee: float
    cost: float
    result: str
    slug: str
    side: str


@dataclass
class Summary:
    trades: list[Trade]
    cumulative: list[float]
    drawdowns: list[float]
    net: float
    fees: float
    stake: float
    wins: int
    losses: int
    win_rate: float | None
    roi: float | None
    max_drawdown: float
    streak_count: int
    streak_result: str
    unrealized: float | None
    marked_positions: int
    unmarked_positions: int
    daily: list[tuple[date, float]]
    timezone_name: str
    stale: bool
    stale_reason: str
    using_last_good: bool
    updated_text: str


def load_paths(root: Path = ROOT) -> tuple[Path, Path, ZoneInfo, str]:
    raw = (root / "config.toml").read_bytes()
    cfg = tomllib.loads(raw.decode("utf-8"))
    app = cfg.get("app") or {}
    name = str(app.get("timezone") or "America/Los_Angeles")
    log_dir = root / str(app.get("log_dir") or "logs")
    return log_dir / "trades.csv", log_dir / "state.json", ZoneInfo(name), name


def read_bytes(path: Path, retries: int = 6, sleep=time.sleep) -> bytes | None:
    """Read a file that the bot may be replacing. A lock is retried; a missing file is empty."""
    if not path.exists():
        return None
    last = None
    delay = 0.02
    for attempt in range(max(1, retries)):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {5, 32}:
                raise
            last = exc
        if attempt + 1 == max(1, retries):
            if last is not None:
                raise last
            return None
        sleep(delay)
        delay = min(delay * 2, 0.25)
    return None


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(text: str, tz: ZoneInfo) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def parse_trades(text: str, tz: ZoneInfo) -> list[Trade]:
    """Official WIN/LOSS rows only. A later official row for the same entry replaces the earlier one."""
    chosen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in csv.DictReader(io.StringIO(text)):
        if not settlement.row_is_official(row):
            continue
        key = (row.get("slug"), row.get("side"), row.get("entry_time_la"))
        if key not in chosen:
            order.append(key)
        chosen[key] = row
    trades: list[Trade] = []
    for key in order:
        row = chosen[key]
        pnl = _as_float(row.get("pnl_usd"))
        if pnl is None:
            continue
        when = _parse_dt(str(row.get("settle_time_la") or ""), tz) or _parse_dt(str(row.get("entry_time_la") or ""), tz)
        if when is None:
            continue
        trades.append(Trade(
            settle=when,
            pnl=pnl,
            fee=_as_float(row.get("fee_usd")) or 0.0,
            cost=_as_float(row.get("cost_usd")) or 0.0,
            result=str(row.get("result") or ""),
            slug=str(row.get("slug") or ""),
            side=str(row.get("side") or ""),
        ))
    trades.sort(key=lambda item: item.settle)
    return trades


def filter_trades(trades: list[Trade], tz: ZoneInfo, day: date | None = None, since: date | None = None) -> list[Trade]:
    kept = []
    for trade in trades:
        local_day = trade.settle.astimezone(tz).date()
        if day is not None and local_day != day:
            continue
        if since is not None and local_day < since:
            continue
        kept.append(trade)
    return kept


def cumulative(pnls: list[float]) -> list[float]:
    total = 0.0
    out = []
    for value in pnls:
        total += value
        out.append(total)
    return out


def drawdowns(curve: list[float]) -> list[float]:
    peak = 0.0
    out = []
    for value in curve:
        peak = max(peak, value)
        out.append(peak - value)
    return out


def current_streak(results: list[str]) -> tuple[int, str]:
    if not results:
        return 0, ""
    last = results[-1]
    count = 0
    for result in reversed(results):
        if result != last:
            break
        count += 1
    return count, last


def daily_pnl(trades: list[Trade], tz: ZoneInfo) -> list[tuple[date, float]]:
    buckets: dict[date, float] = {}
    order: list[date] = []
    for trade in trades:
        day = trade.settle.astimezone(tz).date()
        if day not in buckets:
            order.append(day)
            buckets[day] = 0.0
        buckets[day] += trade.pnl
    return [(day, buckets[day]) for day in order]


def unrealized_from_state(state: dict | None) -> tuple[float | None, int, int]:
    """Mark open positions only when this state file has a bid or an explicit mark.

    Unmarked positions are counted and left out of the total. They are not treated as zero.
    """
    if not state:
        return None, 0, 0
    evaluation = state.get("eval") or {}
    slug = evaluation.get("slug")
    sides = evaluation.get("sides") or {}
    total = 0.0
    marked = 0
    unmarked = 0
    for position in state.get("open_positions") or []:
        mark = None
        if position.get("slug") == slug:
            side = sides.get(position.get("side")) or {}
            mark = side.get("bid")
        if mark is None:
            mark = position.get("mark")
        if mark is None:
            unmarked += 1
            continue
        shares = _as_float(position.get("shares"))
        price = _as_float(mark)
        if shares is None or price is None:
            unmarked += 1
            continue
        cost = _as_float(position.get("cost_usd")) or 0.0
        fee = _as_float(position.get("fee_usd")) or 0.0
        total += shares * price - cost - fee
        marked += 1
    if marked == 0:
        return None, 0, unmarked
    return total, marked, unmarked


def summarize(trades: list[Trade], tz_name: str, tz: ZoneInfo, unrealized: float | None = None,
              marked: int = 0, unmarked: int = 0, stale: bool = False, stale_reason: str = "",
              using_last_good: bool = False, updated_text: str = "") -> Summary:
    curve = cumulative([trade.pnl for trade in trades])
    dds = drawdowns(curve)
    wins = sum(trade.result == "WIN" for trade in trades)
    losses = sum(trade.result == "LOSS" for trade in trades)
    count = len(trades)
    net = curve[-1] if curve else 0.0
    stake = sum(trade.cost for trade in trades)
    streak_count, streak_result = current_streak([trade.result for trade in trades])
    return Summary(
        trades=trades,
        cumulative=curve,
        drawdowns=dds,
        net=net,
        fees=sum(trade.fee for trade in trades),
        stake=stake,
        wins=wins,
        losses=losses,
        win_rate=(wins / count) if count else None,
        roi=(net / stake) if stake else None,
        max_drawdown=max(dds) if dds else 0.0,
        streak_count=streak_count,
        streak_result=streak_result,
        unrealized=unrealized,
        marked_positions=marked,
        unmarked_positions=unmarked,
        daily=daily_pnl(trades, tz),
        timezone_name=tz_name,
        stale=stale,
        stale_reason=stale_reason,
        using_last_good=using_last_good,
        updated_text=updated_text,
    )


def header_lines(summary: Summary) -> list[str]:
    win_rate = "-" if summary.win_rate is None else f"{summary.win_rate:.1%}"
    roi = "-" if summary.roi is None else f"{summary.roi:.1%}"
    if summary.streak_count == 0:
        streak = "-"
    else:
        word = "wins" if summary.streak_result == "WIN" else "losses"
        streak = f"{summary.streak_count} {word}"
    total = summary.net if summary.unrealized is None else summary.net + summary.unrealized
    lines = [
        f"net ${summary.net:.2f}   trades {len(summary.trades)}   win rate {win_rate}   "
        f"fees ${summary.fees:.2f}   ROI {roi}   max drawdown ${summary.max_drawdown:.2f}   streak {streak}",
        f"times in {summary.timezone_name}   updated {summary.updated_text or '-'}",
    ]
    if summary.unrealized is not None:
        lines.append(
            f"unrealized ${summary.unrealized:.2f} on {summary.marked_positions} marked open position(s)"
            f"   total ${total:.2f}"
            + (f"   {summary.unmarked_positions} open position(s) have no mark" if summary.unmarked_positions else "")
        )
    elif summary.unmarked_positions:
        lines.append(f"{summary.unmarked_positions} open position(s) have no mark, so unrealized PnL is not shown")
    if summary.stale:
        lines.append(f"STALE  {summary.stale_reason}")
    if summary.using_last_good:
        lines.append("showing the last read that succeeded; a later read was locked or incomplete")
    return lines


@dataclass
class Book:
    trades: list[Trade] = field(default_factory=list)
    state: dict | None = None
    state_mtime: float | None = None
    csv_error: str = ""
    state_error: str = ""
    using_last_good: bool = False

    def refresh(self, csv_path: Path, state_path: Path, tz: ZoneInfo, sleep=time.sleep) -> None:
        self.using_last_good = False
        try:
            raw = read_bytes(csv_path, sleep=sleep)
        except OSError as exc:
            self.csv_error = type(exc).__name__
            self.using_last_good = True
            raw = None
        if raw is not None:
            try:
                self.trades = parse_trades(raw.decode("utf-8-sig"), tz)
                self.csv_error = ""
            except Exception as exc:
                self.csv_error = type(exc).__name__
                self.using_last_good = True
        try:
            state_raw = read_bytes(state_path, sleep=sleep)
        except OSError as exc:
            self.state_error = type(exc).__name__
            self.using_last_good = True
            state_raw = None
        if state_raw is not None:
            try:
                parsed = json.loads(state_raw.decode("utf-8-sig"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                self.state_error = type(exc).__name__
                self.using_last_good = True
            else:
                if isinstance(parsed, dict):
                    self.state = parsed
                    self.state_error = ""
                    try:
                        self.state_mtime = state_path.stat().st_mtime
                    except OSError:
                        pass
                else:
                    self.state_error = "not_object"
                    self.using_last_good = True


def build_summary(book: Book, tz: ZoneInfo, tz_name: str, day: date | None = None, since: date | None = None,
                  now: float | None = None) -> Summary:
    trades = filter_trades(book.trades, tz, day=day, since=since)
    unrealized, marked, unmarked = unrealized_from_state(book.state)
    now = time.time() if now is None else now
    stale = False
    reason = ""
    if book.state_mtime is None:
        stale = True
        reason = "state file has not been read"
    elif now - book.state_mtime > STALE_AFTER_S:
        stale = True
        reason = f"state file age {now - book.state_mtime:.0f}s"
    if book.state_error and not reason:
        stale = True
        reason = book.state_error
    updated = ""
    if book.state:
        updated = view.clock_text(str(book.state.get("updated_la") or ""))
    return summarize(
        trades, tz_name, tz, unrealized=unrealized, marked=marked, unmarked=unmarked,
        stale=stale, stale_reason=reason, using_last_good=book.using_last_good, updated_text=updated,
    )


def _naive(moment: datetime, tz: ZoneInfo) -> datetime:
    return moment.astimezone(tz).replace(tzinfo=None)


def draw(fig, summary: Summary, tz: ZoneInfo) -> None:
    import matplotlib.dates as mdates

    fig.clear()
    fig.suptitle("\n".join(header_lines(summary)), fontsize=10)
    grid = fig.add_gridspec(3, 2, height_ratios=[1.4, 1.0, 1.0])
    ax_cum = fig.add_subplot(grid[0, :])
    ax_trade = fig.add_subplot(grid[1, 0])
    ax_daily = fig.add_subplot(grid[1, 1])
    ax_dd = fig.add_subplot(grid[2, :])
    if not summary.trades:
        ax_cum.set_title("No official settlements in this range")
        ax_trade.axis("off")
        ax_daily.axis("off")
        ax_dd.axis("off")
        fig.tight_layout()
        return
    when = [_naive(trade.settle, tz) for trade in summary.trades]
    colors = ["#1b7f3b" if trade.result == "WIN" else "#c0392b" for trade in summary.trades]
    ax_cum.plot(when, summary.cumulative, color="#1f4e79", linewidth=1.5)
    ax_cum.scatter(when, summary.cumulative, c=colors, s=28, zorder=3)
    if summary.unrealized is not None:
        ax_cum.scatter([when[-1]], [summary.cumulative[-1] + summary.unrealized], marker="*", s=80, color="#b9770e", zorder=4)
    ax_cum.set_title("Cumulative realized net PnL")
    ax_cum.set_ylabel("USD")
    ax_cum.axhline(0, color="#888888", linewidth=0.6)
    ax_cum.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax_trade.bar(range(1, len(summary.trades) + 1), [trade.pnl for trade in summary.trades], color=colors)
    ax_trade.set_title("Per-trade PnL")
    ax_trade.set_xlabel("trade")
    ax_trade.axhline(0, color="#888888", linewidth=0.6)
    daily_colors = ["#1b7f3b" if value >= 0 else "#c0392b" for _, value in summary.daily]
    ax_daily.bar([day.isoformat() for day, _ in summary.daily], [value for _, value in summary.daily], color=daily_colors)
    ax_daily.set_title("Daily PnL")
    ax_daily.tick_params(axis="x", labelrotation=30)
    ax_daily.axhline(0, color="#888888", linewidth=0.6)
    ax_dd.fill_between(when, summary.drawdowns, color="#8e44ad", alpha=0.35)
    ax_dd.plot(when, summary.drawdowns, color="#8e44ad")
    ax_dd.set_title("Drawdown from the running peak")
    ax_dd.set_ylabel("USD")
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()


def render_png(summary: Summary, tz: ZoneInfo) -> bytes:
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    figure = plt.figure(figsize=(12, 8), dpi=120)
    draw(figure, summary, tz)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(figure)
    return buffer.getvalue()


def save_png(summary: Summary, tz: ZoneInfo, path: Path) -> None:
    path.write_bytes(render_png(summary, tz))


def _page(interval: float) -> bytes:
    millis = int(max(interval, 1.0) * 1000)
    html = f"""<!doctype html>
<meta charset="utf-8">
<title>polybtc PnL</title>
<body style="font-family: sans-serif; margin: 1rem">
<p>Read-only chart. This page does not send orders. Refresh every {interval:g}s.</p>
<img id="chart" alt="PnL chart" src="/pnl.png" style="max-width: 100%">
<script>
setInterval(function () {{
  document.getElementById("chart").src = "/pnl.png?t=" + Date.now();
}}, {millis});
</script>
</body>
"""
    return html.encode("utf-8")


def serve(load_summary, tz: ZoneInfo, host: str = "127.0.0.1", port: int = 8765, interval: float = 5.0):
    page = _page(interval)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body, kind = page, "text/html; charset=utf-8"
            elif path == "/pnl.png":
                body, kind = render_png(load_summary(), tz), "image/png"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    return ThreadingHTTPServer((host, port), Handler)


def parse_day(text: str | None, tz: ZoneInfo) -> date | None:
    if text is None:
        return None
    if text == "today":
        return datetime.now(tz).date()
    return date.fromisoformat(text)


def run_window(load_summary, tz: ZoneInfo, interval: float) -> int:
    import matplotlib
    try:
        matplotlib.use("TkAgg", force=False)
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"No desktop chart window ({type(exc).__name__}). Use: python pnl_chart.py --web")
        return 1
    figure = plt.figure(figsize=(12, 8))
    try:
        while True:
            draw(figure, load_summary(), tz)
            figure.canvas.draw_idle()
            plt.pause(interval)
            if not plt.fignum_exists(figure.number):
                break
    except KeyboardInterrupt:
        pass
    finally:
        plt.close(figure)
    return 0


def main(argv: list[str] | None = None) -> int:
    csv_path, state_path, tz, tz_name = load_paths()
    parser = argparse.ArgumentParser(description="Read-only PnL chart. Does not trade.")
    parser.add_argument("--day", nargs="?", const="today", default=None, help="One config-timezone date, or today when the value is omitted.")
    parser.add_argument("--since", default=None, help="Include settlements on or after this YYYY-MM-DD in the config timezone.")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--png", default=None, help="Write one PNG and exit.")
    parser.add_argument("--web", action="store_true", help="Serve the chart on localhost.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.png and args.web:
        parser.error("use only one of --png and --web")
    if args.interval < 1:
        parser.error("--interval must be at least 1 second")
    try:
        day = parse_day(args.day, tz)
        since = date.fromisoformat(args.since) if args.since else None
    except ValueError:
        print("Dates must be YYYY-MM-DD.", file=sys.stderr)
        return 2
    book = Book()

    def load_summary() -> Summary:
        book.refresh(csv_path, state_path, tz)
        return build_summary(book, tz, tz_name, day=day, since=since)

    if args.png:
        save_png(load_summary(), tz, Path(args.png))
        print(f"wrote {args.png}")
        return 0
    if args.web:
        server = serve(load_summary, tz, port=args.port, interval=args.interval)
        print(f"http://127.0.0.1:{args.port}/  (this machine only; Ctrl+C stops the chart)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    return run_window(load_summary, tz, args.interval)


if __name__ == "__main__":
    sys.exit(main())
