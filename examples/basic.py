"""CPU preparation, eager/graph calls, local files, bash and a native PNG.

Existing configured daemon: python examples/basic.py --output /local/result.png
New installation also needs --new-environment /server/venv,
--comfy-root /server/ComfyUI and --runtime-package /server/comani.whl.
Start the daemon with --no-start when ComfyUI is not installed.
"""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from comani import Client

COMFY_COMMIT = "eee59185ff4f43ae2d9ad122b245619d27d9daad"
CLI_COMMIT = "4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5"


def half_image(image):
    """Pure remote computation; the source-file path is local and uploaded explicitly."""
    return image * 0.5


async def prepare(remote, args):
    info = await remote.comfyui.info()  # Always reads the remote daemon.
    if args.new_environment:
        if not args.comfy_root or not args.runtime_package:
            raise ValueError("New installation needs --comfy-root and --runtime-package")
        if info.state == "running":
            raise ValueError("Stop the runtime explicitly before creating an environment")
        env = await remote.comfyui.create_environment(args.new_environment)
        await remote.comfyui.configure(
            runtime_python=env.python, comfy_root=args.comfy_root, cpu=True, custom_nodes=False,
        )
        await remote.comfyui.prepare_tools(cli_revision=CLI_COMMIT, manager_version="4.2.2")
        await remote.comfyui.install(commit=args.comfy_commit, skip_torch=False)
        await remote.comfyui.prepare_runtime(args.runtime_package)
    elif not info.installed:
        raise ValueError("ComfyUI is absent; select the three new-installation arguments")
    info = await remote.comfyui.info()
    if not info.config.get("cpu"):
        raise ValueError("This example needs configure(cpu=True) while the runtime is stopped")


async def run(args):
    async with Client(args.url, token=os.environ.get("COMANI_TOKEN")) as remote:
        await prepare(remote, args)
        command = await remote.bash.run("printf 'management ready\\n'", check=True)
        print(command.stdout, end="")
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "example.txt"
            local.write_text("A local file, transferred explicitly.\n")
            staged = await remote.files.upload(local)
            try:
                assert (await remote.files.stat(staged)).size == local.stat().st_size
            finally:
                await remote.files.remove(staged)

        await remote.comfyui.start()
        image = remote.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        adjusted = remote.python(source=Path(__file__), entry="half_image",
                                 inputs={"image": image.output()})
        inverted = remote.node("ImageInvert", image=adjusted.output())
        with remote.graph() as workflow:
            image_in = workflow.input("image", type="IMAGE")
            middle, = await remote.node("ImageInvert", image=image_in)
            restored = remote.node("ImageInvert", image=middle)
            workflow.output("image", restored.output())
        submitted = remote.submit(workflow, inputs={"image": inverted.output()})
        intermediate = await submitted[middle]
        saved = remote.node("SaveImage", images=submitted.output("image"), filename_prefix="comani/basic")
        await saved
        metadata = await saved.metadata()
        item = metadata.ui["result"]["images"][0]
        remote_path = str(PurePosixPath(item["subfolder"]) / item["filename"])
        output = await remote.files.stat(remote_path, directory=item["type"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await remote.files.download(output, args.output)
        print(json.dumps({"local_png": str(downloaded.local_path), "sha256": downloaded.sha256,
                          "intermediate_ownership": intermediate.ownership,
                          "remote_png": output.native_name}))
        # SaveImage writes an 8-bit PNG; fetch still preserves exact Tensor values.
        # Generated files persist until removed. This example keeps its PNG.
        if args.stop:
            await remote.comfyui.stop(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/basic.png"),
                        help="local destination; must not exist")
    parser.add_argument("--new-environment", help="new absolute server venv directory")
    parser.add_argument("--comfy-root", help="absolute server ComfyUI source directory")
    parser.add_argument("--runtime-package", help="exact server wheel path or pinned requirement")
    parser.add_argument("--comfy-commit", default=COMFY_COMMIT)
    parser.add_argument("--stop", action="store_true", help="stop compute after saving")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
