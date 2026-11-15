"""Public entrypoints and private native bootstraps exercised as processes."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession

from comani import Client
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor, SupervisorError

PROJECT = Path(__file__).resolve().parents[2]
COMFY = Path(os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI"))
SDK_PYTHON = PROJECT / ".venv-sdk/bin/python"
CONSOLE = next((path for path in (PROJECT / ".venv-sdk/bin/comani",
                                     PROJECT / ".venv-wheel/bin/comani") if path.is_file()),
               PROJECT / ".venv-sdk/bin/comani")
TARGET = PROJECT / ".local/management-implementation/runtime-linked-env/bin/python"


def runtime_python():
    return str(TARGET if TARGET.is_file() else Path(sys.executable))


@pytest.mark.skipif(not SDK_PYTHON.is_file() or not CONSOLE.is_file(), reason="Torch-free SDK environment required")
def test_module_and_console_help_and_effective_config_match(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    commands = [[str(SDK_PYTHON), "-m", "comani"], [str(CONSOLE)]]
    outputs = []
    for command in commands:
        result = subprocess.run([*command, "--help"], cwd=tmp_path, env=env,
                                capture_output=True, text=True, check=True)
        outputs.append(result.stdout.splitlines()[1:])
    assert outputs[0] == outputs[1]
    configs = [json.loads(subprocess.check_output([
        *command, "daemon", "--print-config", "--no-start", "--comfy-root", str(tmp_path / "absent"),
        "--runtime-python", runtime_python(), "--cpu", "--", "--reserve-vram", "4"],
        cwd=tmp_path, env=env, text=True)) for command in commands]
    assert configs[0] == configs[1] and configs[0]["comfy_args"] == ["--reserve-vram", "4"]
    code = "import importlib.util;assert importlib.util.find_spec('torch') is None"
    subprocess.run([str(SDK_PYTHON), "-c", code], env=env, check=True)


@pytest.mark.parametrize("entry", ["module", "console"])
@pytest.mark.skipif(not SDK_PYTHON.is_file() or not CONSOLE.is_file() or not (COMFY / "execution.py").is_file(),
                    reason="Torch-free SDK and native ComfyUI environments required")
async def test_torch_free_management_entry_serves_bash_and_files_without_starting_native(entry, tmp_path):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    command = ([str(SDK_PYTHON), "-m", "comani"] if entry == "module" else [str(CONSOLE)])
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    child = await asyncio.create_subprocess_exec(
        *command, "daemon", "--no-start", "--comfy-root", str(tmp_path / "absent"),
        "--workspace", str(tmp_path), "--runtime-python", runtime_python(), "--port", str(port),
        cwd=tmp_path, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    try:
        async with asyncio.timeout(10):
            while True:
                assert child.returncode is None
                try:
                    async with ClientSession() as http:
                        async with http.post(url + "/manage", json={"method": "handshake", "params": {}}) as response:
                            if response.status == 200:
                                break
                except OSError:
                    pass
                await asyncio.sleep(0.05)
        async with Client(url) as remote:
            assert (await remote.comfyui.info()).state == "stopped"
            assert (await remote.bash.run("printf lower-layer-manager")).stdout == "lower-layer-manager"
            await remote.comfyui.configure(comfy_root=str(COMFY), comfy_args=[
                "--input-directory", str(tmp_path / "input"),
                "--output-directory", str(tmp_path / "output")])
            source = tmp_path / "payload.txt"
            source.write_text("managed files without native boot")
            uploaded = await remote.files.upload(source, path="input.txt")
            copied = await remote.files.download(uploaded, tmp_path / "copy.txt")
            assert copied.local_path.read_text() == source.read_text()
            await remote.files.remove(uploaded)
            assert (await remote.comfyui.info()).state == "stopped"
    finally:
        if child.returncode is None:
            child.terminate()
        try:
            _, errors = await asyncio.wait_for(child.communicate(), 15)
        except TimeoutError:
            child.kill()
            await child.communicate()
            raise AssertionError("Management entrypoint did not clean up")
        assert child.returncode == 0, errors.decode(errors="replace")


@pytest.mark.skipif(not (COMFY / "execution.py").is_file(), reason="real ComfyUI required")
async def test_private_worker_cleanup_and_cold_hot_custom_node_roots(tmp_path):
    roots = [tmp_path / "base/custom_nodes", tmp_path / "extra-nodes"]
    for root in roots:
        plugin = root / "do-not-import"
        plugin.mkdir(parents=True)
        (plugin / "__init__.py").write_text("raise AssertionError('helper imported a plugin')\n")
    extra = tmp_path / "extra.yaml"
    extra.write_text("external:\n  custom_nodes: " + json.dumps(str(roots[1])) + "\n")
    arguments = ("--base-directory", str(tmp_path / "base"), "--extra-model-paths-config", str(extra))
    supervisor = RuntimeSupervisor(RuntimeConfig(str(COMFY), runtime_python=runtime_python(),
        workspace=str(tmp_path), cpu=True, custom_nodes=False, comfy_args=arguments))
    try:
        cold = list((await supervisor.get_file_store()).folder_paths.get_folder_paths("custom_nodes"))
        assert set(map(str, roots)).issubset(cold)
        await supervisor.configure(comfy_args=[*arguments, "--invalid-comani-native-flag"])
        with pytest.raises(SupervisorError, match="exited"):
            await supervisor.start()
        assert supervisor.process.returncode is not None and supervisor.endpoint is None
        await supervisor.configure(comfy_args=arguments)
        starting = asyncio.create_task(supervisor.start())
        async with asyncio.timeout(5):
            while supervisor.process is None or supervisor.process.returncode is not None:
                await asyncio.sleep(0.01)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        assert supervisor.process.returncode is not None and supervisor.endpoint is None
        started = await supervisor.start()
        assert started["state"] == "running"
        hot = list((await supervisor.get_file_store()).folder_paths.get_folder_paths("custom_nodes"))
        assert hot == cold
        handshake = await supervisor.runtime_rpc("handshake")
        session = handshake["session_id"]
        await supervisor.runtime_rpc("submit", {"session_id": session, "call_id": "cpu-node",
            "spec": {"kind": "node", "node_type": "EmptyLatentImage",
                     "inputs": {"width": 64, "height": 64, "batch_size": 1}},
            "submission_window": handshake["submission_window"]})
        assert (await supervisor.runtime_rpc("wait_result", {"session_id": session, "call_id": "cpu-node"}))["outputs"]
        await supervisor.runtime_rpc("close_session", {"session_id": session})
        assert (await supervisor.stop())["state"] == "stopped"
    finally:
        await supervisor.close()
