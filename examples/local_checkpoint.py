"""Generate a PNG using a remote checkpoint or an explicitly uploaded local one.

ComfyUI/Torch must already be configured for the model and desired hardware.
--checkpoint is a server-native filename. --upload-checkpoint is a local file;
--model-root selects its server destination when multiple checkpoint roots exist.
"""

import argparse
import asyncio
import os
from pathlib import Path

import comani


async def main(args):
    async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")) as remote:
        if args.upload_checkpoint:
            roots = await remote.files.roots(category="checkpoints")
            selected = [root for root in roots if root.path == args.model_root] if args.model_root else roots
            if len(selected) != 1:
                raise ValueError(f"Choose --model-root from {[root.path for root in roots]}")
            uploaded = await remote.files.upload(args.upload_checkpoint, root=selected[0],
                                                  path=args.checkpoint)
            checkpoint = uploaded.native_name
        else:
            checkpoint = args.checkpoint
        await remote.comfyui.start()
        with remote.graph() as graph:
            model, clip, vae = remote.node("CheckpointLoaderSimple", ckpt_name=checkpoint)
            positive = remote.node("CLIPTextEncode", text=args.prompt, clip=clip)
            negative = remote.node("CLIPTextEncode", text="blurry, low quality, text, watermark", clip=clip)
            latent = remote.node("EmptyLatentImage", width=512, height=512, batch_size=1)
            sampled = remote.node(
                "KSampler", model=model, seed=args.seed, steps=12, cfg=6.5,
                sampler_name="euler", scheduler="normal", positive=positive,
                negative=negative, latent_image=latent, denoise=1.0,
            )
            decoded = remote.node("VAEDecode", samples=sampled, vae=vae)
            saved = remote.node("SaveImage", images=decoded,
                                filename_prefix="comani/checkpoint", return_call=True)
        run = remote.submit(graph, return_call=True)
        output = await run.file(node_id=saved.id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        print((await remote.files.download(output, args.output)).local_path)
        # The model stays on the server for subsequent runs.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--checkpoint", type=str, required=True, help="server-native checkpoint filename")
    parser.add_argument("--upload-checkpoint", type=Path, help="optional local checkpoint to upload")
    parser.add_argument("--model-root", type=str, help="absolute server checkpoint root for upload")
    parser.add_argument("--prompt", type=str,
                        default="cinematic anime illustration, a red panda in a sunlit garden")
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--output", type=Path, default=Path("artifacts/checkpoint.png"),
                        help="new local PNG; existing files are not overwritten")
    args = parser.parse_args()
    if args.model_root and not args.upload_checkpoint:
        parser.error("--model-root requires --upload-checkpoint")
    asyncio.run(main(args))
