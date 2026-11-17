"""Real out-of-band HTTP, isolated native process, and managed files."""

import asyncio
import os
import socket
import sys
import time
import venv
from pathlib import Path

import pytest
from aiohttp import ClientSession, web

from comfier import Client
from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import RuntimeConfig, RuntimeSupervisor
from comfier.errors import Expired, OperationError, SessionClosed
from comfier.protocol import LZ4_ACCEPT_HEADER, LZ4_CODEC, PICKLE_CONTENT_TYPE

COMFY = os.environ.get("COMFIER_COMFY_ROOT", "/home/delva/projects/ComfyUI")


async def launch(config):
    supervisor = RuntimeSupervisor(config)
    runner = web.AppRunner(create_management_app(supervisor))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return supervisor, runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def test_management_empty_installation_and_operation_survives_client_close(tmp_path):
    supervisor, runner, url = await launch(RuntimeConfig(
        comfy_root=str(tmp_path / "absent"), workspace=str(tmp_path)))
    try:
        async with Client(url) as remote:
            assert supervisor.process is None and not (await remote.comfyui.info()).installed
            with remote.graph():
                command = await remote.bash.run("printf real-management-io")
            assert command.stdout == "real-management-io"
            operation = await remote.bash.begin("sleep 0.1; printf retained")
            handle = operation.export_handle()
        # A fresh management connection observes the same accepted operation.
        async with Client(url) as remote:
            recovered = await remote.operations.get(handle=handle)
            result = await recovered.result()
            assert result.stdout == "retained"
            assert supervisor.process is None
    finally:
        await runner.cleanup()


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_explicit_runtime_lifecycle_binary_proxy_and_offline_files(tmp_path):
    supervisor, runner, url = await launch(RuntimeConfig(
        comfy_root=COMFY, runtime_python=sys.executable, workspace=str(tmp_path),
        cpu=True, custom_nodes=False))
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 12289)
    try:
        async with Client(url, fetch_compression="identity") as remote:
            assert supervisor.process is None
            root, = await remote.files.roots(directory="input")
            staged = await remote.files.upload(source, root=root, path="management-test-" + tmp_path.name + ".bin")
            try:
                downloaded = await remote.files.download(staged, tmp_path / "copied.bin")
                assert downloaded.local_path.read_bytes() == source.read_bytes()
                started = await remote.comfyui.start()
                assert started.state == "running" and started.pid != os.getpid()
                assert started.extra["node_metadata"]["epoch"] == started.runtime_epoch
                assert remote._node_metadata_epoch == started.runtime_epoch
                assert remote.epoch == "" and remote._compute_opening is None
                assert remote._lease is None and remote._heartbeat_task is None
                assert (await supervisor.runtime_rpc("runtime_info"))["sessions"] == 0
                call = remote.node("EmptyLatentImage", return_call=True, width=8192, height=8192, batch_size=1)
                reference = await call.output()
                metadata = await call.metadata()
                assert metadata.prompt_id == call.id and "result" in metadata.executed
                # Both negotiated binary forms traverse the public management
                # endpoint; its process does not decode/re-encode Tensor data.
                params = {"session_id": remote.session_id, "ref_id": reference.id, "epoch": remote.epoch}
                async with ClientSession() as http:
                    for compression in (False, True):
                        headers = {"Accept": PICKLE_CONTENT_TYPE}
                        if compression:
                            headers[LZ4_ACCEPT_HEADER] = LZ4_CODEC
                        async with http.post(url + "/rpc", json={"method": "fetch", "params": params}, headers=headers) as response:
                            assert response.status == 200
                            assert response.content_type.startswith(PICKLE_CONTENT_TYPE)
                            received = 0
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                received += len(chunk)
                            assert received > 1000
                old_epoch = remote.epoch
                assert (await remote.comfyui.stop()).state == "stopped"
                assert (await remote.files.stat(staged)).size == source.stat().st_size
                assert (await remote.bash.run("printf alive")).stdout == "alive"
                restarted = await remote.comfyui.start()
                assert restarted.extra["node_metadata"]["epoch"] == restarted.runtime_epoch
                assert remote._node_metadata_epoch == restarted.runtime_epoch != old_epoch
                assert remote.epoch == "" and remote._compute_opening is None
                assert remote._lease is None and remote._heartbeat_task is None
                assert (await supervisor.runtime_rpc("runtime_info"))["sessions"] == 0
                with pytest.raises(Expired):
                    await reference.fetch()
                next_call = remote.node("EmptyLatentImage", return_call=True, width=64, height=64, batch_size=1)
                await next_call
                assert remote.epoch != old_epoch
            finally:
                await remote.files.remove(staged, missing_ok=True)
    finally:
        await runner.cleanup()


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_missing_target_dependencies_fail_start_without_breaking_management(tmp_path):
    isolated = tmp_path / "target"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(isolated)
    supervisor, runner, url = await launch(RuntimeConfig(
        comfy_root=COMFY, runtime_python=str(isolated / "bin/python"), workspace=str(tmp_path)))
    try:
        async with Client(url) as remote:
            with pytest.raises(OperationError) as failed:
                await remote.comfyui.start()
            assert failed.value.details["error"]["code"] == "RuntimeEnvironmentIncomplete"
            assert supervisor.process is None
            assert (await remote.comfyui.info()).state == "failed"
            assert (await remote.bash.run("printf still-available")).stdout == "still-available"
    finally:
        await runner.cleanup()


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_independent_process_short_grace_heartbeat_survives_blocked_client_loop(tmp_path):
    project = Path(__file__).resolve().parents[2]
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env = dict(os.environ, PYTHONPATH=str(project / "src"))
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "comfier", "daemon", "--comfy-root", COMFY,
        "--workspace", str(tmp_path), "--port", str(port), "--offline-grace", "0.3",
        "--cpu", "--no-custom-nodes", "--no-start", cwd=tmp_path, env=env,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    remote = None
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
        remote = await Client(url).start()
        await remote.comfyui.start()
        call = remote.python(lambda: 12345, return_call=True)
        reference = await call.output()
        assert await reference.fetch() == 12345
        keeper = remote._lease
        assert keeper is not None
        time.sleep(0.9)  # Deliberately block only the SDK business loop: >3x grace.
        assert await reference.fetch() == 12345
        await keeper.close()
        await asyncio.sleep(0.5)
        with pytest.raises(SessionClosed):
            await reference.fetch()
        assert (await remote.comfyui.info()).state == "running"
    finally:
        if remote is not None:
            await remote.close(wait=False)
        if child.returncode is None:
            child.terminate()
        try:
            await asyncio.wait_for(child.communicate(), 15)
        except TimeoutError:
            child.kill()
            await child.communicate()
            raise AssertionError("The independent management daemon failed to clean up")


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_real_http_partition_recovers_original_session_and_side_effect(tmp_path):
    blocked = False
    active = set()

    @web.middleware
    async def network_gate(request, handler):
        transport = request.transport
        if blocked:
            transport.close()
            return web.Response()
        active.add(transport)
        try:
            return await handler(request)
        finally:
            active.discard(transport)

    supervisor = RuntimeSupervisor(RuntimeConfig(COMFY, workspace=str(tmp_path), cpu=True,
                                                 custom_nodes=False, offline_grace=3))
    app = create_management_app(supervisor)
    app.middlewares.insert(0, network_gate)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    source = tmp_path / "partition.py"
    source.write_text("def produce(path):\n    import time\n    with open(path, 'a') as stream: stream.write('once\\n')\n    time.sleep(0.6)\n    return 42\n")
    effect = tmp_path / "effect.txt"
    try:
        async with Client(url) as remote:
            await remote.comfyui.start()
            call = remote.python(source=source, entry="produce", inputs={"path": str(effect)}, return_call=True)
            async with asyncio.timeout(5):
                while not effect.exists():
                    await asyncio.sleep(0.01)
            original_session, original_epoch = remote.session_id, remote.epoch
            blocked = True
            for transport in tuple(active):
                transport.close()

            async def restore_network():
                nonlocal blocked
                await asyncio.sleep(1.1)
                blocked = False

            restore = asyncio.create_task(restore_network())
            try:
                async with asyncio.timeout(8):
                    reference = await call.output()
                    assert await reference.fetch() == 42
            finally:
                await restore
            assert (remote.session_id, remote.epoch) == (original_session, original_epoch)
            assert remote._recovery.generation >= 1
            assert effect.read_text() == "once\n"
    finally:
        blocked = False
        await runner.cleanup()
