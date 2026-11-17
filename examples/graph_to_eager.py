"""Export one graph result and feed it directly into an eager node.

ComfyUI must already be installed and configured. No model or local Torch is
needed. Two ImageInvert nodes approximately restore the original color;
8-bit PNG quantization can differ by one level per channel.
"""

import argparse
import asyncio
import os
from pathlib import Path

import comfier as cf


async def main(args):
    async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
        await cf.comfyui.start()
        source = cf.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        with cf.graph() as workflow:
            image = workflow.input("image", type="IMAGE")
            inverted = cf.node("ImageInvert", image=image)
            workflow.output("image", inverted)

        processed = cf.submit(workflow, inputs={"image": source})
        restored = cf.node("ImageInvert", image=processed)
        saved = cf.node("SaveImage", images=restored,
                            filename_prefix="comfier/graph-to-eager", return_call=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await cf.files.download(await saved.file(), args.output)
        print(downloaded.local_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/graph-to-eager.png"),
                        help="new local PNG; existing files are not overwritten")
    asyncio.run(main(parser.parse_args()))
