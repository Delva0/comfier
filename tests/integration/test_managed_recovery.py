"""Real CPU subprocess and HTTP proof of the unified managed workflow."""

import asyncio
import json
import os
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from comani import Client, Expired
from comani.daemon.management_http import create_management_app
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor

pytestmark = pytest.mark.native
COMFY = Path(os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI"))


@pytest.mark.skipif(not (COMFY / "execution.py").is_file(), reason="Requires a native ComfyUI checkout")
async def test_managed_http_start_lost_replies_and_owned_references(tmp_path):
    supervisor = RuntimeSupervisor(RuntimeConfig(str(COMFY), runtime_python=sys.executable,
        workspace=str(tmp_path), cpu=True, custom_nodes=False))
    runner = web.AppRunner(create_management_app(supervisor))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with Client(url) as remote:
            assert supervisor.process is None
            assert (await remote.comfyui.info()).state == "stopped"
            assert (await remote.bash.run("printf managed-preparation", check=True)).stdout == "managed-preparation"
            await remote.comfyui.start()
            assert remote._recovery is None
            request = remote.transport.request
            dropped, resumes = set(), []

            async def lose_reply(method, **params):
                result = await request(method, **params)
                if method == "resume":
                    resumes.append(params)
                if method in {"submit", "wait_result", "fetch"} and method not in dropped:
                    dropped.add(method)
                    raise aiohttp.ClientConnectionError("Reply lost after real HTTP request completed")
                return result

            remote.transport.request = lose_reply
            effect_file = tmp_path / "effect-count.txt"
            source = """def produce(path):
    with open(path, 'a') as stream:
        stream.write('once\\n')
    return 42
"""
            source_file = tmp_path / "produce.py"
            source_file.write_text(source)
            call = remote.python(source=source_file, entry="produce", inputs={"path": str(effect_file)}, return_call=True)
            first, second = await asyncio.gather(asyncio.ensure_future(call), asyncio.ensure_future(call))
            assert first[0] is second[0]
            assert await first[0].fetch() == 42
            assert effect_file.read_text() == "once\n"
            assert len(resumes) == 3
            assert len({item["session_id"] for item in resumes}) == 1
            assert len({item["epoch"] for item in resumes}) == 1
            image = remote.node("EmptyImage", return_call=True, width=64, height=64, batch_size=1, color=0x123456)
            save = remote.node("SaveImage", {"images": image.output(),
                "filename_prefix": "comani-metadata-" + uuid.uuid4().hex}, return_call=True,
                extra_data={"api_key_comfy_org": "private-metadata-sentinel"})
            metadata = await save.metadata()
            assert isinstance(await save, tuple)
            assert metadata.call_id == save.id and metadata.prompt_id == save.id
            assert "private-metadata-sentinel" not in json.dumps(asdict(metadata))
            saved = metadata.ui["result"]["images"][0]
            relative_path = "/".join(filter(None, [saved["subfolder"], saved["filename"]]))
            output_file = await remote.files.stat(relative_path, directory=saved["type"])
            assert output_file.size > 0
            await remote.files.remove(output_file)
            await remote.comfyui.stop(wait=True)
            with pytest.raises(Expired):
                await first[0].fetch()
    finally:
        await runner.cleanup()
