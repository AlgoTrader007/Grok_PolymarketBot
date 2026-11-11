"""Windows launch files, GitHub test workflow, and the unchanged probability model."""
from pathlib import Path

import model

ROOT = Path(__file__).resolve().parents[1]


def test_model_snapshot_is_unchanged():
    sigma = model.ann_to_sec(0.5)
    probability, method = model.fair_p_up(100000, 100050, 120, sigma)
    assert method == "gbm_twap"
    assert abs(probability - 0.7348986352742222) < 1e-12
    anchored, anchored_method = model.fair_p_up(
        100000, 100050, 30, sigma, twap_now=100020,
        spot_by_sec={second: 100050 for second in range(0, 40)}, t_now=10,
    )
    assert anchored_method == "twap_anchor"
    assert abs(anchored - 0.922185168422864) < 1e-12


def test_windows_scripts_and_workflow_are_editor_independent():
    required = [
        "install.ps1", "run.ps1", "stop.ps1", "clear-kill.ps1", "cancel.ps1",
        "register-task.ps1", "unregister-task.ps1", "start.bat", "stop.bat", ".env.example",
    ]
    for name in required:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "cursor" not in text.lower()
        assert "0x" + "ab" * 8 not in text
    install = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert r"D:\python\Grok_Polymarket" in install
    task = (ROOT / "register-task.ps1").read_text(encoding="utf-8")
    assert "Register-ScheduledTask" in task
    assert "GrokPolymarket" in task
    stop = (ROOT / "stop.ps1").read_text(encoding="utf-8")
    assert "KILL" in stop
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "actions/setup-python" in workflow
    assert "cursor" not in workflow.lower()
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignore and "logs/" in ignore
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("POLYBTC_LIVE_CONFIRM", "POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_ADDRESS"):
        assert name in example
    assert "POLYBTC_LIVE_CONFIRM=YES" not in example
    config = (ROOT / "config.toml").read_text(encoding="utf-8")
    assert "enabled = false" in config
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert r"D:\python\Grok_Polymarket" in readme
    assert "POLYBTC_LIVE_CONFIRM" in readme
    assert "unreliable" in readme.lower()
