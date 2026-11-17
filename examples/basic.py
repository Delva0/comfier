"""Upload a local image, adjust it remotely, save and download a PNG.

ComfyUI must already be installed and configured on the daemon.
Example: python examples/basic.py --input photo.png --output artifacts/basic.png
"""

import argparse
import asyncio
import os
from pathlib import Path

import comfier as cf


async def main(args):
    async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
        await cf.comfyui.start()
        uploaded = await cf.files.upload(args.input)
        try:
            image, _mask = cf.node("LoadImage", image=uploaded.native_name)
            adjusted = cf.python(
                code="def adjust(image):\n    return image * 0.5\n",
                entry="adjust", inputs={"image": image},
            )
            inverted = cf.node("ImageInvert", image=adjusted)
            saved = cf.node("SaveImage", images=inverted,
                                filename_prefix="comfier/basic", return_call=True)
            output = await saved.file()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            downloaded = await cf.files.download(output, args.output)
            print(downloaded.local_path)
        finally:
            # LoadImage has finished before this staged input is removed.
            await cf.files.remove(uploaded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--input", type=Path, required=True, help="local image to upload")
    parser.add_argument("--output", type=Path, default=Path("artifacts/basic.png"),
                        help="new local PNG; existing files are not overwritten")
    asyncio.run(main(parser.parse_args()))
