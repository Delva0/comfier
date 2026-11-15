import os
import subprocess
import sys

import pytest

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(
    not os.path.isfile(COMFY + "/execution.py"), reason="real ComfyUI checkout required",
)


@pytest.mark.parametrize("reserve_argument", [["--reserve-vram", "4"], ["--reserve-vram=4"], ["--reserve-v=4"]])
def test_native_arguments_set_import_time_memory_policy_in_fresh_process(reserve_argument):
    result = subprocess.run(
        [sys.executable, "-c", """
import asyncio
import sys
from comani._runtime import NativeRuntime

async def main():
    original_argv = sys.argv
    runtime = NativeRuntime(sys.argv[1], cpu=True, custom_nodes=False, comfy_args=[
        *sys.argv[2:], "--disable-smart-memory", "--disable-dynamic-vram",
    ])
    try:
        await runtime.start()
        from comfy.cli_args import args
        import comfy.model_management as memory
        assert args.reserve_vram == 4
        assert args.disable_smart_memory
        assert args.disable_dynamic_vram
        assert memory.EXTRA_RESERVED_VRAM == 4 * 1024 ** 3
        assert memory.DISABLE_SMART_MEMORY
        assert sys.argv is original_argv
    finally:
        await runtime.close()

asyncio.run(main())
""", COMFY, *reserve_argument],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("native_args", "message"), [
    (["--unknown-comani-native-option"], "unrecognized arguments"),
    (["--cuda"], "ambiguous option"),
    (["--lowvram", "--highvram"], "not allowed with argument"),
])
def test_native_parser_rejects_invalid_arguments_before_nodes_import(native_args, message):
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from comani._runtime.adapter import NativeAdapter

original_argv = sys.argv
try:
    NativeAdapter(sys.argv[1], custom_nodes=False, comfy_args=sys.argv[2:]).boot()
except SystemExit as error:
    assert error.code == 2
    assert sys.argv is original_argv
    assert "nodes" not in sys.modules
    raise
raise AssertionError("Native ComfyUI parser accepted invalid arguments")
""", COMFY, *native_args],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
