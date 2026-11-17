"""Lifecycle control stays independent of native execution and shell jobs."""

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web

from comfier import Client
from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import RuntimeConfig, RuntimeSupervisor
from comfier.errors import Expired

COMFY = os.environ.get("COMFIER_COMFY_ROOT", "/home/delva/projects/ComfyUI")


async def entered(path):
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_emergency_restart_interrupts_calls_without_stopping_bash_and_default_stop_drains(tmp_path):
    supervisor = RuntimeSupervisor(RuntimeConfig(COMFY, runtime_python=sys.executable,
        workspace=str(tmp_path), cpu=True, custom_nodes=False))
    # A long shell occupies every ordinary worker slot. Lifecycle control must
    # still reach the supervisor rather than queue behind that shell.
    runner = web.AppRunner(create_management_app(supervisor, management_limits={"max_running": 1}))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    source = tmp_path / "native.py"
    source.write_text("def work(marker, duration):\n"
                      "    from pathlib import Path\n"
                      "    import time\n"
                      "    Path(marker + '.entered').write_text('entered')\n"
                      "    time.sleep(duration)\n"
                      "    Path(marker + '.done').write_text('done')\n"
                      "    return 42\n")
    try:
        async with Client(url) as remote:
            await remote.comfyui.start()
            reference = await remote.python(lambda: 123, return_call=True).output()
            assert (await remote.bash.run("printf runtime-is-running")).stdout == "runtime-is-running"
            long_shell = await remote.bash.begin("printf entered > shell.entered; sleep 30")
            await entered(tmp_path / "shell.entered")
            marker = str(tmp_path / "interrupted")
            running = remote.python(source=source, entry="work", inputs={"marker": marker, "duration": 30}, return_call=True)
            await entered(Path(marker + ".entered"))
            queued_marker = str(tmp_path / "queued")
            queued = remote.python(source=source, entry="work", inputs={"marker": queued_marker, "duration": 0}, return_call=True)
            async with asyncio.timeout(3):
                while (await supervisor.runtime_rpc("runtime_info"))["pending"] < 2:
                    await asyncio.sleep(0.01)
            started = time.monotonic()
            restarted = await asyncio.wait_for(remote.comfyui.restart(wait=False), 20)
            assert restarted.state == "running" and time.monotonic() - started < 20
            assert not Path(marker + ".done").exists()
            assert not Path(queued_marker + ".entered").exists()
            for call in (running, queued):
                with pytest.raises(Expired):
                    await asyncio.wait_for(call.output(), 3)
            with pytest.raises(Expired):
                await reference.fetch()
            assert (await long_shell.status()).state == "running"
            assert (await remote.comfyui.info()).state == "running"

            marker = str(tmp_path / "drained")
            remote.python(source=source, entry="work", inputs={"marker": marker, "duration": 1}, return_call=True)
            await entered(Path(marker + ".entered"))
            queued_marker = str(tmp_path / "drained-queue")
            remote.python(source=source, entry="work", inputs={"marker": queued_marker, "duration": 0.1}, return_call=True)
            stop = await remote.comfyui.begin_stop()
            await asyncio.sleep(0.1)
            assert not (await stop.status()).done
            assert (await remote.comfyui.info()).state == "running"
            assert (await asyncio.wait_for(stop.result(), 15)).state == "stopped"
            assert Path(marker + ".done").exists() and Path(queued_marker + ".done").exists()
            await long_shell.cancel()
    finally:
        await runner.cleanup()
