"""Viewer refresh, state-file age, and a writer that survives a locked replace."""
import asyncio
import json
import os
from pathlib import Path

import persist
import view


def test_clock_keeps_the_numeric_offset_and_does_not_say_pt():
    assert view.clock_text("2026-11-11T03:20:04.123-08:00") == "03:20:04-08:00"
    assert view.clock_text("2026-11-11T06:20:04.123456-05:00") == "06:20:04-05:00"
    assert "PT" not in view.clock_text("2026-11-11T03:20:04.123-08:00")
    assert view.state_age_seconds(Path("missing-state.json"), now=10) is None


def test_diag_reports_path_progress_and_hides_extra_fields(tmp_path):
    path = tmp_path / "state.json"
    secret = "super-secret-private-key-value"
    path.write_text(json.dumps({
        "pid": os.getpid(), "mode": "paper", "live_reason": "live_disabled", "kill": False,
        "updated_la": "2026-11-11T03:20:04.000-08:00", "uptime_s": 12, "n_trades": 0,
        "private_note": secret,
    }), encoding="utf-8")
    still = view.diagnose(path, samples=2, interval=1, sleep=lambda _seconds: None)
    assert "writer: mtime did not change" in still
    assert str(path.resolve()) in still
    assert secret not in still
    assert "orders: not sent" in still
    assert view.pid_alive(os.getpid()) is True

    def advance(_seconds):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["uptime_s"] = int(payload["uptime_s"]) + 1
        path.write_text(json.dumps(payload), encoding="utf-8")

    moving = view.diagnose(path, samples=2, interval=1, sleep=advance)
    assert "writer: advancing" in moving
    assert secret not in moving


def test_locked_replace_is_retried_and_other_errors_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(persist.time, "sleep", lambda _seconds: None)
    target = tmp_path / "state.json"
    calls = {"n": 0}
    real = persist.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        real(src, dst)

    monkeypatch.setattr(persist.os, "replace", flaky)
    persist.atomic_write(target, {"ok": True})
    assert calls["n"] == 3
    assert json.loads(target.read_text(encoding="utf-8"))["ok"] is True

    def broken(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(persist.os, "replace", broken)
    try:
        persist.atomic_write(tmp_path / "other.json", {"ok": False})
    except OSError as exc:
        assert "disk" in str(exc)
    else:
        raise AssertionError("expected the non-lock error to propagate")
    assert persist.replace_is_locked(PermissionError("busy"))
    locked = OSError("denied")
    locked.winerror = 32
    assert persist.replace_is_locked(locked)
    assert not persist.replace_is_locked(OSError("disk"))


def test_view_repaints_from_the_calling_loop(monkeypatch):
    class FakeLive:
        kwargs = None
        updates = []
        stops = 0

        def __init__(self, frame, **kwargs):
            FakeLive.kwargs = kwargs

        def start(self):
            return None

        def stop(self):
            FakeLive.stops += 1

        def update(self, frame, refresh=False):
            FakeLive.updates.append(refresh)
            if len(FakeLive.updates) == 1:
                raise RuntimeError("console write failed")
            if len(FakeLive.updates) >= 3:
                raise KeyboardInterrupt

    monkeypatch.setattr(view, "render", lambda path: "frame")
    import importlib
    live_mod = importlib.import_module("rich.live")
    monkeypatch.setattr(live_mod, "Live", FakeLive)
    view.run_live(Path("unused"), sleep=lambda _seconds: None)
    assert FakeLive.kwargs["auto_refresh"] is False
    assert FakeLive.kwargs["redirect_stdout"] is False
    assert FakeLive.updates == [True, True, True]
    assert FakeLive.stops >= 1


def test_housekeeping_keeps_writing_after_a_state_error():
    import bot
    calls = {"n": 0}
    previous = bot.S.shutdown

    def write(path, obj):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("busy")
        bot.S.shutdown = True

    original = bot.persist.atomic_write
    bot.persist.atomic_write = write
    bot.S.shutdown = False
    try:
        asyncio.run(bot.housekeeping())
    finally:
        bot.persist.atomic_write = original
        bot.S.shutdown = previous
    assert calls["n"] >= 2
