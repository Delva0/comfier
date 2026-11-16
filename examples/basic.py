"""Upload a local image, adjust it remotely, save and download a PNG.

ComfyUI must already be installed and configured on the daemon.
Example: python examples/basic.py --input photo.png --output artifacts/basic.png
"""

import argparse
import asyncio
import os
from pathlib import Path

import comani


async def main(args):
    async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
        await comani.comfyui.start()
        uploaded = await comani.files.upload(args.input)
        try:
            image, _mask = comani.node("LoadImage", image=uploaded.native_name)
            adjusted = comani.python(
                code="def adjust(image):\n    return image * 0.5\n",
                entry="adjust", inputs={"image": image},
            )
            inverted = comani.node("ImageInvert", image=adjusted)
            saved = comani.node("SaveImage", images=inverted,
                                filename_prefix="comani/basic", return_call=True)
            output = await saved.file()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            downloaded = await comani.files.download(output, args.output)
            print(downloaded.local_path)
        finally:
            # LoadImage has finished before this staged input is removed.
            await comani.files.remove(uploaded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--input", type=Path, required=True, help="local image to upload")
    parser.add_argument("--output", type=Path, default=Path("artifacts/basic.png"),
                        help="new local PNG; existing files are not overwritten")
    asyncio.run(main(parser.parse_args()))
