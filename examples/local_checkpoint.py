"""Generate a native 8-bit PNG from an existing or explicitly uploaded checkpoint.

--checkpoint is a server-native model name. --upload-checkpoint is a local file
and needs --model-root: an absolute server root returned by files.roots.
ComfyUI/Torch must already be configured for the desired hardware.
"""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path, PurePosixPath

from comani import Client


async def main(args):
    started = time.perf_counter()
    async with Client(args.url, token=os.environ.get("COMANI_TOKEN")) as remote:
        info = await remote.comfyui.info()
        if not info.installed:
            raise ValueError("Install/configure ComfyUI first; basic.py shows CPU preparation")
        if args.upload_checkpoint:
            roots = await remote.files.roots(category="checkpoints")
            selected = [root for root in roots if root.path == args.model_root]
            if len(selected) != 1:
                raise ValueError(f"Choose --model-root from {[root.path for root in roots]}")
            uploaded = await remote.files.upload(args.upload_checkpoint, root=selected[0],
                                                 path=args.checkpoint)
            checkpoint = uploaded.native_name
        else:
            listed = await remote.models.list(category="checkpoints", prefix=args.checkpoint)
            if args.checkpoint not in [file["native_name"] for file in listed["files"]]:
                raise ValueError(f"Checkpoint is absent in the remote roots: {args.checkpoint}")
            checkpoint = args.checkpoint
        if info.state != "running":
            await remote.comfyui.start()
        with remote.graph() as graph:
            model, clip, vae = remote.node("CheckpointLoaderSimple", ckpt_name=checkpoint)
            positive = remote.node("CLIPTextEncode", text=args.prompt, clip=clip)
            negative = remote.node("CLIPTextEncode", text="blurry, low quality, text, watermark",
                                   clip=clip)
            latent = remote.node("EmptyLatentImage", width=512, height=512, batch_size=1)
            sampled = remote.node(
                "KSampler", model=model, seed=args.seed, steps=12, cfg=6.5,
                sampler_name="euler", scheduler="normal", positive=positive,
                negative=negative, latent_image=latent, denoise=1.0,
            )
            decoded = remote.node("VAEDecode", samples=sampled, vae=vae)
            saved = remote.node("SaveImage", images=decoded,
                                filename_prefix="comani/checkpoint", return_call=True)
            graph.target(saved)
        run = remote.submit(graph)
        await run
        metadata = await run.metadata()
        item = metadata.ui[saved.id]["images"][0]
        remote_path = str(PurePosixPath(item["subfolder"]) / item["filename"])
        file = await remote.files.stat(remote_path, directory=item["type"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        downloaded = await remote.files.download(file, args.output)
        record = {"model": checkpoint, "seed": args.seed, "prompt": args.prompt,
                  "seconds": round(time.perf_counter() - started, 3), "workflow_id": run.id,
                  "png_sha256": downloaded.sha256, "output": str(downloaded.local_path)}
        args.output.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--checkpoint", required=True, help="server-native checkpoint filename")
    parser.add_argument("--upload-checkpoint", type=Path, help="optional local checkpoint file")
    parser.add_argument("--model-root", help="absolute server checkpoints root for upload")
    parser.add_argument("--output", type=Path, default=Path("artifacts/local/checkpoint.png"))
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--prompt", default="cinematic anime illustration, a red panda in a sunlit garden")
    asyncio.run(main(parser.parse_args()))
