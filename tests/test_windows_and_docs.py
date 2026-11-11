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


def test_python_commands_replace_shell_scripts():
    assert list(ROOT.rglob("*.ps1")) == []
    assert list(ROOT.rglob("*.bat")) == []
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    for banned in (".ps1", ".bat", "Activate.ps1", "powershell"):
        assert banned.lower() not in readme.lower()
        assert banned.lower() not in workflow.lower()
    assert r"D:\python\Grok_Polymarket" in readme
    assert r".\.venv\Scripts\python.exe -m pip install -r requirements.txt" in readme
    assert "bot.py --check" in readme
    assert "bot.py --kill" in readme
    assert "bot.py --clear-kill" in readme
    assert "bot.py --cancel-all" in readme
    assert "Ctrl+C" in readme
    assert "Task Scheduler" in readme
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "cursor" not in example.lower()
    assert "windows-latest" in workflow
    assert "ubuntu-latest" in workflow
    assert "actions/setup-python" in workflow
    assert "cursor" not in workflow.lower()
    assert "python -m pytest -q" in workflow
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


def test_check_and_kill_commands_do_not_trade(tmp_path):
    import bot
    secret = "super-secret-private-key-value"
    proxy = "http://user:password@127.0.0.1:9"
    code, text = bot.format_check(
        bot.CFG,
        {"POLYMARKET_PRIVATE_KEY": secret, "HTTPS_PROXY": proxy},
        kill_exists=False,
        env_file_exists=False,
    )
    assert code == 0
    assert "mode: paper" in text
    assert "orders: not sent" in text
    assert secret not in text
    assert proxy not in text
    blocked, blocked_text = bot.format_check(
        {"live": {"enabled": True}},
        {"POLYBTC_LIVE_CONFIRM": "YES", "POLYMARKET_PRIVATE_KEY": secret},
        kill_exists=False,
        env_file_exists=True,
    )
    assert blocked == 1
    assert "mode: live_blocked" in blocked_text
    assert "wallet_missing" in blocked_text
    assert secret not in blocked_text
    kill = tmp_path / "logs" / "KILL"
    bot.write_kill_file(kill)
    assert kill.read_text(encoding="utf-8") == "kill\n"
    assert bot.clear_kill_file(kill) is True
    assert bot.clear_kill_file(kill) is False
