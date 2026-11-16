"""Export one graph result and feed it directly into an eager node.

ComfyUI must already be installed and configured. No model or local Torch is
needed. Two ImageInvert nodes approximately restore the original color;
8-bit PNG quantization can differ by one level per channel.
"""

import argparse
import asyncio
import os
from pathlib import Path

import comani


async def main(args):
    async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
        await comani.comfyui.start()
        source = comani.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        with comani.graph() as workflow:
            image = workflow.input("image", type="IMAGE")
            inverted = comani.node("ImageInvert", image=image)
            workflow.output("image", inverted)

        processed = comani.submit(workflow, inputs={"image": source})
        restored = comani.node("ImageInvert", image=processed)
        saved = comani.node("SaveImage", images=restored,
                            filename_prefix="comani/graph-to-eager", return_call=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await comani.files.download(await saved.file(), args.output)
        print(downloaded.local_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/graph-to-eager.png"),
                        help="new local PNG; existing files are not overwritten")
    asyncio.run(main(parser.parse_args()))
