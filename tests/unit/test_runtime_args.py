import argparse
import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest

from comani.cli import daemon as launcher
from comani.daemon import NativeRuntime
from comani.daemon.adapter import NativeAdapter
from comani.daemon.service import RecoveryPolicy
from comani.daemon.startup import initialize


@pytest.mark.parametrize("native_args", [[], ["--reserve-vram", "4", "--disable-smart-memory"]])
def test_private_worker_receives_native_arguments_without_a_second_cli(monkeypatch, tmp_path, native_args):
    import importlib.util

    from aiohttp import web

    from comani.daemon import _worker, http, service

    captured = {}
    runtime, app = object(), object()

    def make_runtime(root, **kwargs):
        captured.update(root=root, **kwargs)
        return runtime

    async def make_app(value, **kwargs):
        assert value is runtime and kwargs == {"token": "test-token"}
        return app

    def run_app(value, **kwargs):
        assert asyncio.run(value) is app and kwargs == {"host": "127.0.0.1", "port": 8190}

    monkeypatch.setattr(service, "NativeRuntime", make_runtime)
    monkeypatch.setattr(http, "create_runtime_app", make_app)
    monkeypatch.setattr(web, "run_app", run_app)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: object())
    (tmp_path / "execution.py").touch()
    _worker.run({"comfy_root": str(tmp_path), "cpu": True,
                 "custom_nodes": False, "comfy_args": native_args},
                port=8190, token="test-token")
    assert captured == {
        "root": tmp_path, "cpu": True, "custom_nodes": False, "comfy_args": native_args,
        "max_file_bytes": None, "recovery_policy": RecoveryPolicy(),
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


@pytest.fixture
def startup_parser(monkeypatch, tmp_path):
    parser = argparse.ArgumentParser()
    parser.add_argument("--reserve-vram", type=float)
    parser.add_argument("--name")
    parser.add_argument("-l", "--listen", nargs="?", const="0.0.0.0", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("-d", "--cuda-device")
    parser.add_argument("--default-device", type=int)
    parser.add_argument("--oneapi-device-selector")
    parser.add_argument("--deterministic", action="store_true")
    allocator = parser.add_mutually_exclusive_group()
    allocator.add_argument("--cuda-malloc", action="store_true")
    allocator.add_argument("--disable-cuda-malloc", action="store_true")
    parser.set_defaults(cache_none=False, whitelist_custom_nodes=[])
    args = parser.parse_args([])
    # Upstream cuda_malloc changes the shared args after automatic detection.
    args.cuda_malloc = True
    monkeypatch.setitem(sys.modules, "comfy.cli_args", SimpleNamespace(parser=parser, args=args))
    monkeypatch.setitem(sys.modules, "torch", object())
    monkeypatch.delitem(sys.modules, "main", raising=False)
    imports = []
    native = SimpleNamespace(folder_paths=SimpleNamespace(get_temp_directory=lambda: str(tmp_path)))

    def import_native(name):
        imports.append(name)
        if name == "cuda_malloc":
            return SimpleNamespace(get_torch_version_noimport=lambda: "2.10.0")
        assert name == "main"
        return native

    monkeypatch.setattr("comani.daemon.startup.importlib.import_module", import_native)
    args.temp_directory = None
    return parser, args, imports, tmp_path


@pytest.mark.parametrize("arguments", [
    [], ["--reserve-vram", "4"], ["--reserve-v=4"], ["--name=--listen"],
])
def test_startup_explicit_detection_preserves_native_defaults(startup_parser, arguments):
    parser, args, imports, root = startup_parser
    defaults = copy.deepcopy(vars(parser.parse_args([])))
    values = copy.deepcopy(vars(args))
    for _ in range(3):
        assert initialize(root, arguments, custom_nodes=False)[1] is args
    assert imports == ["cuda_malloc", "main"] * 3
    assert vars(parser.parse_args([])) == defaults
    assert vars(args) == values


@pytest.mark.parametrize("arguments", [
    ["--listen"], ["--listen=127.0.0.1"], ["--liste=127.0.0.1"],
    ["-l", "127.0.0.1"], ["--port=8188"],
])
def test_startup_rejects_explicit_headless_options_before_import(startup_parser, arguments, capsys):
    _, _, imports, root = startup_parser
    with pytest.raises(SystemExit) as error:
        initialize(root, arguments, custom_nodes=False)
    assert error.value.code == 2
    assert "Comani does not host" in capsys.readouterr().err
    assert imports == []


@pytest.mark.parametrize("arguments", [
    ["--cuda-device", "0"], ["--cuda-dev=0"], ["-d0"], ["--default-device=0"],
    ["--oneapi-device-selector=level_zero:gpu"], ["--deterministic"],
    ["--cuda-malloc"], ["--disable-cuda-malloc"],
])
def test_startup_guards_only_explicit_device_options(startup_parser, arguments):
    _, _, imports, root = startup_parser
    with pytest.raises(RuntimeError, match="before importing torch"):
        initialize(root, arguments, custom_nodes=False)
    assert imports == []


@pytest.mark.parametrize(("arguments", "message"), [
    (["--unknown-option"], "unrecognized arguments"),
    (["--cuda"], "ambiguous option"),
    (["--cuda-malloc", "--disable-cuda-malloc"], "not allowed with argument"),
])
def test_startup_keeps_native_parser_errors(startup_parser, arguments, message, capsys):
    _, _, imports, root = startup_parser
    with pytest.raises(SystemExit) as error:
        initialize(root, arguments, custom_nodes=False)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    assert imports == []
