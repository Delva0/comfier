import os
from pathlib import Path

import pytest
from aiohttp import web

from comani._runtime import NativeRuntime
from comani._runtime.http import create_app

PROJECT = Path(__file__).resolve().parents[2]
COMFY_ROOT = Path(os.environ.get("COMANI_COMFY_ROOT", PROJECT.parent / "ComfyUI"))


@pytest.fixture
async def native_http(request):
    if not (COMFY_ROOT / "execution.py").is_file():
        pytest.skip("Set COMANI_COMFY_ROOT to the supported ComfyUI checkout")
    options = getattr(request, "param", None)
    options = options if isinstance(options, dict) else {"token": options}
    runtime = NativeRuntime(COMFY_ROOT, cpu=True, custom_nodes=False,
                            session_ttl=options.get("session_ttl", 120))
    runner = web.AppRunner(create_app(runtime, token=options.get("token")))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield runtime, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()
