"""Local CPU regression for lossless Python inputs and native dependencies."""

import os
from pathlib import Path

import pytest

from comfier import Client, InProcessTransport
from comfier.daemon import NativeRuntime

COMFY = os.environ.get("COMFIER_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(),
                                reason="real ComfyUI required")


async def test_native_python_input_identity_cycles_closure_and_graph_ports(tmp_path):
    runtime = await NativeRuntime(
        COMFY, cpu=True, custom_nodes=False,
        comfy_args=("--disable-partner-nodes", "--base-directory", str(tmp_path)),
    ).start()
    try:
        async with Client(transport=InProcessTransport(runtime)) as remote:
            shared = {"answer": 42}
            recursive = {"first": shared, "second": shared}
            recursive["self"] = recursive
            result = remote.python(
                lambda first, *, second, tree: (
                    first is second, tree["first"] is tree["second"], tree["self"] is tree,
                ), args=(shared,), inputs={"second": shared, "tree": recursive},
            )
            assert await (await result).fetch() == (True, True, True)

            captured = 7

            def closure(number):
                return number + captured

            value = remote.python(lambda function: function(3), inputs={"function": closure})
            assert await (await value).fetch() == 10

            producer = remote.python(lambda: {"answer": 42})
            with remote.graph() as graph:
                binding = graph.input("value")
                tree = {"first": binding, "second": binding}
                tree["self"] = tree
                output = remote.python(
                    lambda tree: (tree["self"] is tree, tree["first"] is tree["second"],
                                  tree["first"]["answer"]), inputs={"tree": tree},
                )
                graph.output("result", output)
            result = remote.submit(graph, inputs={"value": producer})
            assert await (await result).fetch() == (True, True, 42)
    finally:
        await runtime.close()
