import os

import pytest

from comfier import __main__ as cli
from comfier.cli import daemon as launcher


def test_public_cli_dispatches_in_current_process(monkeypatch):
    captured = []
    monkeypatch.setattr(launcher, "main", lambda argv: captured.append((os.getpid(), argv)))
    arguments = ["--comfy-root", "/configured/ComfyUI", "--cpu", "--", "--reserve-vram", "4"]
    cli.main(["daemon", *arguments])
    assert captured == [(os.getpid(), arguments)]


def test_public_cli_help_and_unknown_command(capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["--help"])
    assert result.value.code == 0
    assert "daemon" in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        cli.main(["install"])
    assert result.value.code == 2


@pytest.mark.parametrize("port", ["-1", "65536"])
def test_daemon_rejects_invalid_port_before_runtime(port, capsys):
    with pytest.raises(SystemExit) as result:
        cli.main(["daemon", "--comfy-root", "/configured/ComfyUI", "--port", port])
    assert result.value.code == 2
    assert "--port must be between" in capsys.readouterr().err


@pytest.mark.parametrize("current, expected", [(None, "legacy"), ("current", "current"), ("", "")])
def test_daemon_environment_prefers_comfier_and_keeps_legacy_fallback(monkeypatch, current, expected):
    captured = {}
    for suffix in ("TOKEN", "ZMQ_CURVE_PUBLIC_KEY", "ZMQ_CURVE_SECRET_KEY"):
        monkeypatch.setenv("COMANI_" + suffix, "legacy")
        if current is None:
            monkeypatch.delenv("COMFIER_" + suffix, raising=False)
        else:
            monkeypatch.setenv("COMFIER_" + suffix, current)

    def create_app(supervisor, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(launcher, "create_management_app", create_app)
    monkeypatch.setattr(launcher.web, "run_app", lambda *args, **kwargs: None)
    launcher.main(["--comfy-root", "/configured/ComfyUI", "--no-start"])
    assert captured["token"] == expected
    assert captured["curve_public_key"] == expected
    assert captured["curve_secret_key"] == expected
