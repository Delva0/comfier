import sys

import pytest

from comani._runtime import NativeRuntime
from comani._runtime import __main__ as launcher
from comani._runtime.adapter import NativeAdapter


@pytest.mark.parametrize("native_args", [[], ["--reserve-vram", "4", "--disable-smart-memory"]])
def test_launcher_forwards_native_arguments_after_separator(monkeypatch, tmp_path, native_args):
    captured = {}
    runtime = object()
    app = object()

    def make_runtime(root, **kwargs):
        captured.update(root=root, **kwargs)
        return runtime

    def make_app(value, **kwargs):
        assert value is runtime
        assert kwargs == {"token": "test-token"}
        return app

    def run_app(value, **kwargs):
        assert value is app
        assert kwargs == {"host": "127.0.0.1", "port": 8190}

    monkeypatch.setattr(launcher, "NativeRuntime", make_runtime)
    monkeypatch.setattr(launcher, "create_app", make_app)
    monkeypatch.setattr(launcher.web, "run_app", run_app)
    monkeypatch.setattr(launcher.importlib.util, "find_spec", lambda _: object())
    (tmp_path / "execution.py").touch()
    launcher.main([
        "--comfy-root", str(tmp_path), "--cpu", "--no-custom-nodes", "--token", "test-token",
        *(["--", *native_args] if native_args else []),
    ])
    assert captured == {
        "root": tmp_path, "cpu": True, "custom_nodes": False, "comfy_args": native_args,
    }


def test_launcher_rejects_unknown_driver_arguments_before_separator(capsys):
    with pytest.raises(SystemExit) as error:
        launcher.main(["--comfy-root", "/native", "--reserve-vram", "4"])
    assert error.value.code == 2
    assert "unrecognized arguments: --reserve-vram" in capsys.readouterr().err


def test_runtime_copies_native_arguments_for_startup():
    native_args = ["--reserve-vram", "4"]
    runtime = NativeRuntime("/native", comfy_args=native_args)
    try:
        native_args[1] = "0"
        assert runtime.adapter.comfy_args == ("--reserve-vram", "4")
    finally:
        runtime.pool.shutdown()


def test_native_arguments_cannot_silently_reconfigure_imported_comfy(monkeypatch, tmp_path):
    (tmp_path / "execution.py").touch()
    monkeypatch.setitem(sys.modules, "comfy.cli_args", object())
    original_argv = sys.argv
    adapter = NativeAdapter(tmp_path, comfy_args=["--reserve-vram", "4"])
    with pytest.raises(RuntimeError, match="require a fresh process"):
        adapter.boot()
    assert sys.argv is original_argv
