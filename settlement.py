"""Official settlement and restart restore.

A position stays open until Gamma reports an official Up/Down result.
A missing close, a feed-only TWAP, or an UNKNOWN row is not a loss.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

OFFICIAL_SOURCES = {"gamma_official", "gamma_outcome_prices", "gamma_final_price"}
CSV_FIELDS = [
    "entry_time_la", "settle_time_la", "slug", "side", "ask_vwap", "best_ask", "fee_usd", "fee_per_share",
    "fair_prob", "edge", "shares", "cost_usd", "price_to_beat", "final_price", "result", "pnl_usd",
    "settle_source", "secs_left_at_entry", "sigma_ann", "partial",
]


def official_outcome(closed, outcome_prices, meta) -> tuple[str | None, str]:
    """Return (Up|Down|None, reason). Conflicting official fields stay unresolved."""
    from_prices = None
    prices = outcome_prices
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = None
    if closed and isinstance(prices, (list, tuple)) and len(prices) >= 2:
        try:
            nums = [float(x) for x in prices[:2]]
        except (TypeError, ValueError):
            nums = []
        if len(nums) == 2 and max(nums) > 0.99:
            if nums[0] > 0.5 and nums[1] < 0.5:
                from_prices = "Up"
            elif nums[1] > 0.5 and nums[0] < 0.5:
                from_prices = "Down"
    from_meta = None
    meta = meta or {}
    final_price = meta.get("finalPrice")
    price_to_beat = meta.get("priceToBeat")
    if final_price is not None and price_to_beat is not None and str(final_price) != "" and str(price_to_beat) != "":
        try:
            from_meta = "Up" if float(final_price) >= float(price_to_beat) else "Down"
        except (TypeError, ValueError):
            from_meta = None
    if from_prices and from_meta and from_prices != from_meta:
        return None, "official_conflict"
    if from_prices and from_meta:
        return from_prices, "gamma_official"
    if from_prices:
        return from_prices, "gamma_outcome_prices"
    if from_meta:
        return from_meta, "gamma_final_price"
    return None, "unresolved"


def settlement_pnl(position: dict, outcome: str | None) -> dict | None:
    """PnL for an official outcome. None means the position stays open and unrealized."""
    if outcome not in ("Up", "Down"):
        return None
    shares = float(position.get("shares") or 0)
    cost = float(position.get("cost_usd") or 0)
    fee = float(position.get("fee_usd") or 0)
    win = outcome == position.get("side")
    pnl = (shares if win else 0.0) - cost - fee
    return {
        "result": "WIN" if win else "LOSS",
        "pnl_usd": round(pnl, 4),
        "win": win,
    }


def position_key(row: dict) -> tuple:
    return (row.get("slug"), row.get("side"), row.get("entry_ts") or row.get("entry_time_la"))


def row_is_official(row: dict) -> bool:
    return row.get("result") in ("WIN", "LOSS") and row.get("settle_source") in OFFICIAL_SOURCES


def start_from_slug(slug: str | None) -> int | None:
    if not slug or not str(slug).startswith("btc-updown-5m-"):
        return None
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except ValueError:
        return None


def _csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _jsonl(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def restore_from_logs(log_dir: str | Path) -> dict:
    """Rebuild open positions and realized PnL.

    Official WIN/LOSS rows count. UNKNOWN rows and feed-only settlements do not,
    and those entries are returned as still open so an official result can be retried.
    There is no age cutoff.
    """
    log_dir = Path(log_dir)
    official_keys = set()
    realized = 0.0
    n_trades = 0
    n_wins = 0
    csv_open = {}
    for row in _csv_rows(log_dir / "trades.csv"):
        key = position_key(row)
        if row_is_official(row):
            official_keys.add(key)
            try:
                realized += float(row.get("pnl_usd") or 0)
            except (TypeError, ValueError):
                pass
            n_trades += 1
            n_wins += row.get("result") == "WIN"
            csv_open.pop(key, None)
        else:
            if key not in official_keys:
                csv_open[key] = row
    entries = {}
    for record in _jsonl(log_dir / "trades.jsonl"):
        kind = record.get("type")
        key = position_key(record)
        if kind in ("paper_entry", "live_entry"):
            entries[key] = record
        elif kind in ("paper_settle", "live_settle") and row_is_official(record):
            official_keys.add(key)
    positions = []
    seen = set()
    for key, record in entries.items():
        if key in official_keys or key in seen:
            continue
        seen.add(key)
        positions.append(_position_from_record(record))
    for key, row in csv_open.items():
        if key in official_keys or key in seen:
            continue
        seen.add(key)
        positions.append(_position_from_record(row))
    return {
        "positions": positions,
        "realized": round(realized, 4),
        "n_trades": n_trades,
        "n_wins": n_wins,
    }


def _position_from_record(record: dict) -> dict:
    start = record.get("start")
    if start is None:
        start = start_from_slug(record.get("slug"))
    end = record.get("end")
    if end is None and start is not None:
        end = int(start) + 300
    position = {k: v for k, v in record.items() if k not in ("type", "ts", "ts_la")}
    position["start"] = int(start) if start is not None else None
    position["end"] = int(end) if end is not None else None
    position["settlement"] = "unresolved"
    for name in ("shares", "cost_usd", "fee_usd", "ask_vwap", "fair_prob", "edge"):
        if position.get(name) not in (None, ""):
            try:
                position[name] = float(position[name])
            except (TypeError, ValueError):
                pass
    return position


def append_csv(path: str | Path, row: dict) -> bool:
    """Append one official settlement. Returns False when that key was already official."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _csv_rows(path)
    key = position_key(row)
    if any(position_key(item) == key and row_is_official(item) for item in existing):
        return False
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({name: row.get(name) for name in CSV_FIELDS})
    return True
