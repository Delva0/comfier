"""Generate one local-model PNG through comani; no model downloads.

Run from this project with its GPU daemon already started:
  .venv/bin/python -B examples/local_checkpoint.py
This uses AstralSea, not the separately accepted remote MiniMax model.
"""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from comani import Client


def encode_png(image):
    import io

    import numpy as np
    import torch
    from PIL import Image

    pixels = image[0].detach().float().cpu().numpy()
    assert np.isfinite(pixels).all()
    buffer = io.BytesIO()
    Image.fromarray((np.clip(pixels, 0, 1) * 255).round().astype(np.uint8)).save(buffer, "PNG")
    return {"png": buffer.getvalue(), "shape": list(image.shape),
            "gpu": torch.cuda.get_device_name(0),
            "daemon_peak_cuda_allocated_bytes": torch.cuda.max_memory_allocated()}


async def main(args):
    started = time.perf_counter()
    async with asyncio.timeout(300), Client(args.url) as remote:
        with remote.graph() as graph:
            model, clip, vae = await remote.node("CheckpointLoaderSimple", ckpt_name=args.checkpoint)
            positive = remote.node("CLIPTextEncode", text=args.prompt, clip=clip)
            negative = remote.node("CLIPTextEncode", text="blurry, low quality, text, watermark", clip=clip)
            latent = remote.node("EmptyLatentImage", width=512, height=512, batch_size=1)
            sampled = remote.node(
                "KSampler", model=model, seed=20261114, steps=12, cfg=6.5,
                sampler_name="euler", scheduler="normal", positive=positive.output(),
                negative=negative.output(), latent_image=latent.output(), denoise=1.0,
            )
            decoded = remote.node("VAEDecode", samples=sampled.output(), vae=vae)
            graph.output("image", decoded.output())
        run = remote.submit(graph)
        # Only this explicit eager consumer temporarily retains the borrowed
        # image. Models stay under native cache/offload management.
        encoded = remote.python(encode_png, inputs={"image": run.output("image")})
        result = await (await encoded.output()).fetch()
        png = result.pop("png")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(png)
        result.update(model=args.checkpoint, prompt=args.prompt, seed=20261114,
                      steps=12, cfg=6.5, sampler="euler", scheduler="normal",
                      source="LOCAL existing AstralSea checkpoint; not remote MiniMax acceptance",
                      seconds=round(time.perf_counter() - started, 3), png_sha256=hashlib.sha256(png).hexdigest(),
                      png_bytes=len(png), workflow_id=run.id, output=str(args.output.resolve()))
        args.output.with_suffix(".json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8190")
    parser.add_argument("--checkpoint", default="astralsea_v12.safetensors")
    parser.add_argument("--output", type=Path, default=Path("artifacts/local/astralsea-local-512.png"))
    parser.add_argument("--prompt", default="cinematic anime illustration, a small red panda sitting in a sunlit Japanese garden, maple leaves, detailed fur, warm colors, high quality")
    asyncio.run(main(parser.parse_args()))
