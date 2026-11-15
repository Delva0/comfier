import os

import pytest

from comani import __main__ as cli
from comani.cli import daemon as launcher


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
