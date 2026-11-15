#!/usr/bin/env python3
"""Summarize logs/skips.jsonl. Read-only: no orders and no secrets.

Days follow timezone in config.toml. Set that to America/New_York for US Eastern.

  python skip_report.py
  python skip_report.py --day 2026-11-13
  python skip_report.py --since 2026-11-13

With no date, the report is today in the config timezone. --day is that one
calendar day. --since includes that day and every later day. Pass both and a
row must match the day and fall on or after --since.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent


def config_timezone(config_path: Path | None = None) -> tuple[str, ZoneInfo]:
    path = config_path or (HERE / "config.toml")
    with path.open("rb") as handle:
        cfg = tomllib.load(handle)
    name = str((cfg.get("app") or {}).get("timezone") or "America/Los_Angeles")
    return name, ZoneInfo(name)


def load_skips(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def row_day(row: dict, tz: ZoneInfo) -> str | None:
    text = str(row.get("ts_local") or row.get("ts_la") or "")
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    ts = row.get("ts")
    if ts in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def reason_codes(row: dict) -> list[str]:
    raw = row.get("reasons")
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw if part]
    return []


def select_rows(rows: list[dict], *, day: str | None, since: str | None, tz: ZoneInfo) -> list[dict]:
    kept = []
    for row in rows:
        stamp = row_day(row, tz)
        if stamp is None:
            continue
        if day and stamp != day:
            continue
        if since and stamp < since:
            continue
        kept.append(row)
    return kept


def render_report(rows: list[dict], *, day: str | None, since: str | None, tz_name: str, tz: ZoneInfo) -> str:
    lines = []
    if day and since:
        lines.append(f"skip reasons for {day} since {since} ({tz_name})")
    elif day:
        lines.append(f"skip reasons for {day} ({tz_name})")
    elif since:
        lines.append(f"skip reasons since {since} ({tz_name})")
    else:
        lines.append(f"skip reasons ({tz_name})")
    lines.append(f"rows: {len(rows)}")
    by_reason: Counter = Counter()
    by_day: dict[str, Counter] = defaultdict(Counter)
    by_window: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for row in rows:
        stamp = row_day(row, tz) or "?"
        codes = reason_codes(row)
        slug = str(row.get("slug") or "?")
        side = str(row.get("side") or "?")
        if not codes:
            by_reason["(none)"] += 1
            by_day[stamp]["(none)"] += 1
            by_window[slug][side]["(none)"] += 1
            continue
        for code in codes:
            by_reason[code] += 1
            by_day[stamp][code] += 1
            by_window[slug][side][code] += 1
    lines.append("")
    lines.append("by reason:")
    if not by_reason:
        lines.append("  (none)")
    for code, count in sorted(by_reason.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"  {code}: {count}")
    lines.append("")
    lines.append("by day:")
    if not by_day:
        lines.append("  (none)")
    for stamp in sorted(by_day):
        detail = ", ".join(f"{code} {by_day[stamp][code]}" for code in sorted(by_day[stamp]))
        lines.append(f"  {stamp}: {detail}")
    lines.append("")
    lines.append("by window:")
    if not by_window:
        lines.append("  (none)")
    for slug in sorted(by_window):
        lines.append(f"  {slug}")
        for side in sorted(by_window[slug]):
            detail = ", ".join(f"{code} {by_window[slug][side][code]}" for code in sorted(by_window[slug][side]))
            lines.append(f"    {side}: {detail}")
    lines.append("orders: not sent")
    return "\n".join(lines)


def report_text(path: Path, *, day: str | None, since: str | None, config_path: Path | None = None) -> str:
    tz_name, tz = config_timezone(config_path)
    rows = select_rows(load_skips(path), day=day, since=since, tz=tz)
    return render_report(rows, day=day, since=since, tz_name=tz_name, tz=tz)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Summarize logs/skips.jsonl. Does not trade.")
    parser.add_argument("--day", help="One calendar day YYYY-MM-DD in the config timezone.")
    parser.add_argument("--since", help="Include this YYYY-MM-DD and later days in the config timezone.")
    args = parser.parse_args(argv)
    tz_name, tz = config_timezone()
    day = args.day
    since = args.since
    if day is None and since is None:
        day = datetime.now(tz).date().isoformat()
    text = report_text(HERE / "logs" / "skips.jsonl", day=day, since=since)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
