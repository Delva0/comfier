"""Keep model bytes outside code and verify reuse across real local lifecycles."""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web

import comani

pytestmark = pytest.mark.native
PROJECT = Path(__file__).resolve().parents[2]
COMFY = Path(os.environ.get("COMANI_COMFY_ROOT", PROJECT.parent / "ComfyUI"))
RUNTIME_PYTHON = os.environ.get("COMANI_NATIVE_PYTHON", sys.executable)
NAME = "comani-persistence-probe.safetensors"
HEADER = json.dumps({"probe": {"dtype": "F32", "shape": [1],
                              "data_offsets": [0, 4]}}, separators=(",", ":")).encode()
HEADER += b" " * (-len(HEADER) % 8)
PAYLOAD = struct.pack("<Q", len(HEADER)) + HEADER + struct.pack("<f", 1.25)
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
READ_NATIVE = """def main(name):
    import hashlib
    import os
    import folder_paths
    import execution
    from safetensors import safe_open
    path = folder_paths.get_full_path('checkpoints', name)
    with safe_open(path, framework='pt', device='cpu') as model:
        value = model.get_tensor('probe').item()
    with open(path, 'rb') as model:
        digest = hashlib.sha256(model.read()).hexdigest()
    return {'path': path, 'sha256': digest, 'value': value,
            'pid': os.getpid(), 'folder_paths_file': folder_paths.__file__,
            'execution_file': execution.__file__,
            'listed': name in folder_paths.get_filename_list('checkpoints')}
"""


@asynccontextmanager
async def daemon(config_path, log_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = dict(os.environ)
    environment.pop("COMANI_TOKEN", None)
    environment.pop("COMANI_RUNTIME_TOKEN", None)
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "comani", "daemon", "--config", str(config_path),
             "--host", "127.0.0.1", "--port", str(port), "--no-start"],
            cwd=PROJECT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text(errors="replace"))
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    await asyncio.sleep(0.1)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
            else:
                raise AssertionError("Local management daemon did not become ready")
            yield f"http://127.0.0.1:{port}", process.pid
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=10)


def copy_code(destination):
    """Copy tracked Comfy code, excluding untracked model caches and .git."""
    result = subprocess.run(["git", "ls-files", "-z"], cwd=COMFY,
                            check=True, capture_output=True)
    count = 0
    for relative in result.stdout.decode().split("\0"):
        if not relative:
            continue
        source = COMFY / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    assert (destination / "execution.py").is_file()
    return count


async def test_model_bytes_survive_restart_and_code_redeployment(tmp_path):
    if not (COMFY / "execution.py").is_file() or not Path(RUNTIME_PYTHON).is_file():
        pytest.skip("An existing native ComfyUI checkout and Python are required")
    data = tmp_path / "persistent-data"
    model_directory = data / "models/checkpoints"
    model_directory.mkdir(parents=True)
    for name in ("input", "output", "temp", "user"):
        (data / name).mkdir()
    paths = data / "extra_model_paths.yaml"
    paths.write_text("persistent:\n  base_path: " + json.dumps(str(data))
                     + "\n  is_default: true\n  checkpoints: models/checkpoints\n")
    config = {
        "comfy_root": str(COMFY), "runtime_python": RUNTIME_PYTHON,
        "workspace": str(data), "cpu": True, "custom_nodes": False,
        "comfy_args": ["--disable-partner-nodes", "--extra-model-paths-config", str(paths),
                       "--input-directory", str(data / "input"),
                       "--output-directory", str(data / "output"),
                       "--temp-directory", str(data / "temp"),
                       "--user-directory", str(data / "user")],
    }
    config_path = data / "runtime.json"
    config_path.write_text(json.dumps(config, indent=2))
    source_gets = []

    async def model_source(request):
        source_gets.append(request.path)
        return web.Response(body=PAYLOAD)

    source_app = web.Application()
    source_app.router.add_get("/model", model_source)
    source = web.AppRunner(source_app)
    await source.setup()
    site = web.TCPSite(source, "127.0.0.1", 0)
    await site.start()
    source_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]) + "/model"
    artifact = {"provider": "url", "source": {"url": source_url}, "filename": NAME,
                "size": len(PAYLOAD), "sha256": DIGEST}
    report = {"scope": "Real local CLI daemon and CPU ComfyUI; tiny valid safetensors payload, "
                       "native path resolution and CPU tensor read; no GPU or cloud requests",
              "payload_bytes": len(PAYLOAD), "payload_sha256": DIGEST,
              "phases": [], "native_reads": []}

    async def reuse(remote, phase):
        before = len(source_gets)
        roots = await remote.files.roots(category="checkpoints")
        assert Path(roots[0].path) == model_directory
        file = await remote.models.download(artifact, root=roots[0], path=NAME,
                                            reuse=True, transport="http")
        assert Path(file.root.path) == model_directory and file.sha256 == DIGEST
        assert (model_directory / NAME).read_bytes() == PAYLOAD
        report["phases"].append({"name": phase, "source_gets": len(source_gets) - before,
                                 "model_path": str(model_directory / NAME), "sha256": file.sha256})

    async def native_read(remote, phase, expected_code):
        output = await remote.python(code=READ_NATIVE, entry="main", inputs={"name": NAME})
        read = await output.fetch()
        assert Path(read["path"]) == model_directory / NAME
        assert read["sha256"] == DIGEST and read["listed"] and read["value"] == 1.25
        assert Path(read["folder_paths_file"]).resolve().parent == expected_code.resolve()
        assert Path(read["execution_file"]).resolve().parent == expected_code.resolve()
        report["native_reads"].append({"phase": phase, **read})

    try:
        async with daemon(config_path, tmp_path / "daemon-first.log") as (url, first_pid):
            async with comani.Client(url) as remote:
                await reuse(remote, "first_download_while_runtime_stopped")
                assert len(source_gets) == 1
                first = await remote.comfyui.start()
                await native_read(remote, "first_runtime", COMFY)
                restarted = await remote.comfyui.restart()
                assert restarted.runtime_epoch != first.runtime_epoch
                assert restarted.pid != first.pid
                report["runtime_restart_pids"] = [first.pid, restarted.pid]
                await reuse(remote, "runtime_restart")
                await native_read(remote, "runtime_restart", COMFY)

        async with daemon(config_path, tmp_path / "daemon-second.log") as (url, second_pid):
            assert second_pid != first_pid
            report["fresh_daemon_process"] = True
            report["daemon_pids"] = [first_pid, second_pid]
            async with comani.Client(url) as remote:
                assert (await remote.comfyui.info()).state == "stopped"
                await reuse(remote, "daemon_restart_before_runtime_start")
                second = await remote.comfyui.start()
                await native_read(remote, "daemon_restart", COMFY)
                await remote.comfyui.stop()
                next_code = tmp_path / "redeployed-code"
                report["copied_code_files"] = await asyncio.to_thread(copy_code, next_code)
                assert model_directory.is_relative_to(data)
                assert not model_directory.is_relative_to(next_code)
                await remote.comfyui.configure(comfy_root=str(next_code))
                saved = json.loads(config_path.read_text())
                assert saved["comfy_root"] == str(next_code)
                assert saved["comfy_args"] == config["comfy_args"]
                await reuse(remote, "new_code_directory_before_runtime_start")
                redeployed = await remote.comfyui.start()
                assert redeployed.pid != second.pid
                report["code_redeployment_pids"] = [second.pid, redeployed.pid]
                await native_read(remote, "code_redeployment", next_code)
                await reuse(remote, "new_code_directory_running")
                await remote.comfyui.stop()
        assert [phase["source_gets"] for phase in report["phases"]] == [1, 0, 0, 0, 0]
        report["total_source_gets"] = len(source_gets)
        report["passed"] = True
    finally:
        await source.cleanup()
        directory = Path(os.environ.get("COMANI_MODEL_PERSISTENCE_REPORT_DIR", tmp_path))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
