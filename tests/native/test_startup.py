import os
import subprocess
import sys
from pathlib import Path

import pytest

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(
    not Path(COMFY, "main.py").is_file(), reason="configured ComfyUI environment required",
)


def probe(source, *arguments, env=None):
    result = subprocess.run([sys.executable, "-B", "-c", source, COMFY, *arguments],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("arguments", "kind", "cache_args"), [
    (["--cache-classic"], "CLASSIC", {"lru": 0, "ram": 0, "ram_inactive": 0}),
    (["--cache-lru", "3"], "LRU", {"lru": 3, "ram": 0, "ram_inactive": 0}),
    (["--cache-ram", "0.125", "0.25"], "RAM_PRESSURE", {"lru": 0, "ram": 0.125, "ram_inactive": 0.25}),
])
def test_native_cache_options_run_eager_and_borrowed_graph(arguments, kind, cache_args):
    import json

    probe("""
import asyncio, json, sys
from comani import Client, InProcessTransport
from comani.daemon import NativeRuntime

async def main():
    runtime = NativeRuntime(sys.argv[1], cpu=True, custom_nodes=False,
                            comfy_args=['--disable-partner-nodes', *json.loads(sys.argv[2])])
    try:
        async with Client(transport=InProcessTransport(runtime)) as remote:
            assert runtime.adapter.executor.cache_type.name == sys.argv[3]
            assert runtime.adapter.executor.cache_args == json.loads(sys.argv[4])
            first = remote.python(lambda: 3, return_call=True)
            second = remote.python(lambda value: value * 7, inputs={'value': first.output()}, return_call=True)
            assert await (await second.output()).fetch() == 21
            with remote.graph() as graph:
                middle, = await remote.python(lambda: 6, return_call=True)
                final = remote.python(lambda value: value * 7, inputs={'value': middle}, return_call=True)
                graph.output('answer', final.output())
            run = remote.submit(graph, return_call=True)
            ref = await run.output('answer')
            assert ref.ownership == 'borrowed'
            assert await ref.fetch() == 42
    finally:
        await runtime.close()

asyncio.run(main())
""", json.dumps(arguments), kind, json.dumps(cache_args))


def test_native_paths_apply_before_registry_initialization(tmp_path):
    paths = {name: tmp_path / name for name in ("base", "models", "input", "output", "user", "previews", "temporary", "extra")}
    for path in paths.values():
        path.mkdir()
    (paths["extra"] / "checkpoints").mkdir()
    config = tmp_path / "models.yaml"
    config.write_text(f"extra:\n  base_path: {paths['extra']}\n  checkpoints: checkpoints\n")
    arguments = ["--base-directory", str(paths["base"]), "--extra-model-paths-config", str(config)]
    for name in ("models", "input", "output", "user", "previews"):
        arguments.extend([f"--{name}-directory", str(paths[name])])
    arguments.extend(["--temp-directory", str(paths["temporary"])])
    import json

    probe("""
import json, sys
from pathlib import Path
from comani.daemon.adapter import NativeAdapter
root = Path(sys.argv[2])
adapter = NativeAdapter(sys.argv[1], cpu=True, custom_nodes=False,
                        comfy_args=['--disable-partner-nodes', *json.loads(sys.argv[3])])
try:
    adapter.boot()
    import folder_paths as paths
    assert paths.base_path == str(root / 'base')
    assert paths.models_dir == str(root / 'models')
    for name in ['input', 'output', 'user', 'previews']:
        assert getattr(paths, 'get_' + name + '_directory')() == str(root / name)
    assert paths.get_temp_directory() == str(root / 'temporary' / 'temp')
    assert str(root / 'extra' / 'checkpoints') in paths.get_folder_paths('checkpoints')
finally:
    adapter.close()
""", str(tmp_path), json.dumps(arguments))


def test_sequential_default_runtimes_do_not_treat_native_auto_detection_as_new_options():
    probe("""
import asyncio, sys
from comani import Client, InProcessTransport
from comani.daemon import NativeRuntime

async def main():
    for expected in (17, 23, 29):
        runtime = NativeRuntime(sys.argv[1], cpu=True, custom_nodes=False)
        try:
            async with Client(transport=InProcessTransport(runtime)) as remote:
                result = remote.python(lambda value: value, inputs={'value': expected}, return_call=True)
                assert await (await result.output()).fetch() == expected
        finally:
            await runtime.close()

asyncio.run(main())
""")


def test_device_environment_is_applied_before_first_torch_import():
    probe("""
import importlib.abc, os, sys
captured = []
class ObserveTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'torch':
            from importlib.machinery import PathFinder
            spec = PathFinder.find_spec(fullname, path)
            original = spec.loader
            class ObserveLoader:
                def create_module(self, spec):
                    return original.create_module(spec)
                def exec_module(self, module):
                    captured.append({key: os.environ.get(key) for key in [
                        'CUDA_VISIBLE_DEVICES', 'HIP_VISIBLE_DEVICES', 'ASCEND_RT_VISIBLE_DEVICES',
                        'ONEAPI_DEVICE_SELECTOR', 'CUBLAS_WORKSPACE_CONFIG',
                    ]})
                    return original.exec_module(module)
            spec.loader = ObserveLoader()
            return spec
sys.meta_path.insert(0, ObserveTorch())
os.environ.pop('CUBLAS_WORKSPACE_CONFIG', None)
from comani.daemon.adapter import NativeAdapter
adapter = NativeAdapter(sys.argv[1], cpu=True, custom_nodes=False, comfy_args=[
    '--disable-partner-nodes', '--cuda-device', '3',
    '--oneapi-device-selector', 'level_zero:gpu', '--deterministic',
])
try:
    adapter.boot()
    assert captured == [{
        'CUDA_VISIBLE_DEVICES': '3', 'HIP_VISIBLE_DEVICES': '3', 'ASCEND_RT_VISIBLE_DEVICES': '3',
        'ONEAPI_DEVICE_SELECTOR': 'level_zero:gpu', 'CUBLAS_WORKSPACE_CONFIG': ':4096:8',
    }], captured
finally:
    adapter.close()
""")


@pytest.mark.parametrize("disabled", [False, True])
def test_partner_registry_follows_native_configuration_without_network(disabled):
    probe("""
import socket, sys
def denied(*args, **kwargs):
    raise AssertionError('Registry initialization attempted an outbound connection')
socket.socket.connect = denied
from comani.daemon.adapter import NativeAdapter
disabled = sys.argv[2] == 'True'
adapter = NativeAdapter(sys.argv[1], cpu=True, custom_nodes=False,
                        comfy_args=['--disable-partner-nodes'] if disabled else [])
try:
    adapter.boot()
    assert ('OpenAIChatNode' in adapter.nodes.NODE_CLASS_MAPPINGS) is not disabled
finally:
    adapter.close()
""", str(disabled))


@pytest.mark.parametrize("arguments", [
    ["--cache-none"], ["--listen", "0.0.0.0"], ["--port", "9001"], ["--auto-launch"],
    ["--enable-assets"], ["--enable-manager"], ["--enable-agent"],
    ["--liste=127.0.0.1"], ["--port=8188"],
])
def test_unsupported_native_services_fail_before_torch_import(arguments):
    result = subprocess.run([sys.executable, "-B", "-c", """
import sys
from comani.daemon.adapter import NativeAdapter
try:
    NativeAdapter(sys.argv[1], cpu=True, custom_nodes=False, comfy_args=sys.argv[2:]).boot()
except SystemExit as error:
    assert error.code == 2
    assert 'torch' not in sys.modules
    raise
raise AssertionError('Unsupported native mode was accepted')
""", COMFY, *arguments], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Comani" in result.stderr
