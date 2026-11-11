#!/usr/bin/env python3
"""Terminal view of logs/state.json. Ctrl-C exits the viewer only.

The file is next to this script, not the shell's current directory.
`view.py --diag` prints a few read-only samples of that file and does not
read environment variables or send orders.
"""
import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / "logs" / "state.json"
STALE_AFTER_S = 5.0


def clock_text(iso_text: str) -> str:
    """Return the time of day plus its numeric UTC offset.

    The suffix is the offset stored in the timestamp. It is not renamed to PT or ET.
    """
    if not iso_text or "T" not in iso_text:
        return iso_text or "-"
    clock = iso_text.split("T", 1)[1]
    if "." not in clock:
        return clock
    head, tail = clock.split(".", 1)
    offset = ""
    for index, char in enumerate(tail):
        if char in "+-":
            offset = tail[index:]
            break
        if char == "Z":
            offset = "Z"
            break
    return head + offset


def state_age_seconds(path: Path, now: float | None = None) -> float | None:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - modified


def pid_alive(pid) -> bool | None:
    """Whether a pid exists. On Windows this only queries the process; it does not signal it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def _pid_alive_windows(pid: int) -> bool | None:
    import ctypes
    access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = ctypes.windll.kernel32.OpenProcess(access, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    error = ctypes.windll.kernel32.GetLastError()
    if error == 5:  # access denied: the process exists
        return True
    if error in {87, 6}:
        return False
    return False


def read_state(path: Path = STATE_PATH):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def sample_state(path: Path = STATE_PATH) -> dict:
    info = {"path": str(path.resolve()), "exists": path.exists()}
    if not info["exists"]:
        return info
    try:
        stat = path.stat()
    except OSError as exc:
        info["read_error"] = type(exc).__name__
        return info
    info["size"] = stat.st_size
    info["mtime_unix"] = stat.st_mtime
    info["mtime_ns"] = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    info["mtime_age_s"] = round(time.time() - stat.st_mtime, 3)
    try:
        data = read_state(path)
    except (OSError, json.JSONDecodeError) as exc:
        info["read_error"] = type(exc).__name__
        return info
    for key in ("pid", "mode", "live_reason", "kill", "updated_la", "updated_unix", "uptime_s", "n_trades"):
        if key in data:
            info[key] = data.get(key)
    info["pid_alive"] = pid_alive(data.get("pid"))
    info["clock"] = clock_text(str(data.get("updated_la") or ""))
    return info


def diagnose(path: Path = STATE_PATH, samples: int = 3, interval: float = 1.0, sleep=time.sleep) -> str:
    """Sample the state file. Does not read the environment and does not trade."""
    rows = []
    for index in range(max(1, samples)):
        rows.append(sample_state(path))
        if index + 1 < samples:
            sleep(interval)
    lines = [f"path: {rows[0].get('path')}", f"exists: {rows[0].get('exists')}"]
    if not rows[0].get("exists"):
        lines.append("writer: state file is missing")
    else:
        last = rows[-1]
        for key in ("size", "mtime_age_s", "pid", "pid_alive", "mode", "live_reason", "kill", "clock", "uptime_s", "n_trades", "read_error"):
            if key in last:
                lines.append(f"{key}: {last.get(key)}")
        markers = [(row.get("mtime_ns"), row.get("uptime_s"), row.get("clock"), row.get("updated_unix")) for row in rows]
        if len(set(markers)) > 1:
            lines.append("writer: advancing")
        else:
            lines.append("writer: mtime did not change during the sample")
    lines.append("orders: not sent")
    return "\n".join(lines)


def f(value, digits=4):
    return "-" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def render(path: Path = STATE_PATH):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table

    age = state_age_seconds(path)
    age_text = "missing" if age is None else f"{age:.1f}s"
    stale = age is None or age > STALE_AFTER_S
    try:
        state = read_state(path)
    except Exception as exc:
        return Panel(f"waiting for {path}: {type(exc).__name__}: {exc}\nstate file age {age_text}")
    evaluation = state.get("eval") or {}
    fresh = "STALE" if stale else "fresh"
    head = (
        f"[b]{evaluation.get('slug', '-')}[/b]  left {f(evaluation.get('secs_left'), 0)}s   "
        f"spot {f(evaluation.get('spot'), 2)}  TWAP60 {f(evaluation.get('twap'), 2)}  "
        f"to-beat {f(evaluation.get('K'), 2)}\n"
        f"sigma {f(evaluation.get('sigma_ann'), 3)} ann ({state.get('sigma_src')})  "
        f"model {evaluation.get('method', '-')}  feed lag {f(evaluation.get('feed_lag'), 1)}s  "
        f"blocked {evaluation.get('blocked') or evaluation.get('status') or '-'}\n"
        f"mode {state.get('mode', 'paper')}  reason {state.get('live_reason', '')}  kill {state.get('kill')}  "
        f"PnL ${state.get('realized_pnl', 0):.2f}  trades {state.get('n_trades')}  wins {state.get('n_wins')}  "
        f"updated {clock_text(str(state.get('updated_la') or ''))}  pid {state.get('pid')}\n"
        f"state file age {age_text} {fresh}  {path}"
    )
    sides = Table(title="Sides", expand=True)
    for column in ("side", "fair", "bid", "ask", "ask size", "fee/sh", "edge", "book age"):
        sides.add_column(column)
    for side, detail in (evaluation.get("sides") or {}).items():
        edge = detail.get("edge")
        shown = f(edge, 3)
        if edge is not None and edge >= 0.05:
            shown = f"[green]{shown}[/green]"
        sides.add_row(
            side, f(detail.get("fair"), 3), f(detail.get("bid"), 2), f(detail.get("ask"), 2),
            f(detail.get("ask_size"), 1), f(detail.get("fee_ps"), 4), shown, f(detail.get("book_age"), 1),
        )
    mode = state.get("mode") or "paper"
    title = "polybtc — PAPER" if mode == "paper" else f"polybtc — {mode.upper()}"
    if stale:
        title += " — state file stale"
    positions = Table(title="Open positions (official settlement still pending)", expand=True)
    for column in ("slug", "side", "shares", "vwap", "fee", "fair", "edge", "entry"):
        positions.add_column(column)
    for position in state.get("open_positions") or []:
        positions.add_row(
            str(position.get("slug") or "")[-10:], str(position.get("side") or ""),
            f(position.get("shares"), 2), f(position.get("ask_vwap"), 3), f(position.get("fee_usd"), 3),
            f(position.get("fair_prob"), 3), f(position.get("edge"), 3),
            clock_text(str(position.get("entry_time_la") or "")),
        )
    return Group(Panel(head, title=title), sides, positions)


def run_live(path: Path = STATE_PATH, sleep=time.sleep) -> None:
    from rich.live import Live

    live = None
    try:
        while True:
            frame = render(path)
            try:
                if live is None:
                    live = Live(
                        frame, auto_refresh=False, screen=False,
                        redirect_stdout=False, redirect_stderr=False,
                    )
                    live.start()
                live.update(frame, refresh=True)
            except KeyboardInterrupt:
                raise
            except Exception:
                if live is not None:
                    try:
                        live.stop()
                    except Exception:
                        pass
                    live = None
            sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if live is not None:
            try:
                live.stop()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read logs/state.json. Does not trade.")
    parser.add_argument("--diag", action="store_true", help="Print a few read-only samples of the state file and exit.")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.diag:
        print(diagnose(STATE_PATH, samples=args.samples, interval=args.interval))
        return 0
    run_live(STATE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
