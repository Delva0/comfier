"""Restore a photo in Qwen 2.1 tiles, then blend the tiles back together.

Eager adaptation of AusBoss v1.1: https://civitai.com/models/2988629 .
Needs current ComfyUI + ComfyUI-AusBoss, Qwen 2.1, Restore and Viggle LoRAs.
The base model and both LoRAs retain the Qwen Research License.

python examples/qwen_tiled_upscale.py --input small-photo.png --megapixels 4 \
    --output-dir artifacts/qwen-tiled
Use --no-turbo --steps 25 for the workflow's slower branch.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import comfier as cf

MODEL = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
RESTORE = "qwen-image-2.1-restore.safetensors"
TURBO = "Qwen-Image-2.1-viggle-turbo-v0.3-6step-lora-r128.safetensors"
WEIGHTS = (
    ("diffusion_models", "Comfy-Org/Qwen-Image-2.1", "diffusion_models/" + MODEL),
    ("text_encoders", "Comfy-Org/Qwen-Image-2.1", "text_encoders/" + CLIP),
    ("vae", "Comfy-Org/Qwen-Image-2.1", "vae/" + VAE),
    ("loras", "ausboss/Qwen-Image-2.1-Restore-LoRA", RESTORE),
    ("loras", "Viggle/Qwen-Image-2.1-viggle-turbo", TURBO),
)
PROMPT = ("Restore the photo: remove blur, noise and compression artifacts and recover sharp, "
          "natural detail, keeping the same composition, people and colors.")


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass  # Check file creation before starting remote work.
    print(f"Output directory: {args.output_dir}", flush=True)
    profile = {"workflow": "https://civitai.com/models/2988629", "phases": [], "arguments": {
        key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}}
    stages = profile["phases"]
    total_started = time.perf_counter()
    try:
        async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
            started = time.perf_counter()
            if args.install_plugin:
                installed = await cf.plugins.list()
                if not any(
                    plugin.id.casefold() in {"ausboss-nodes", "comfyui-ausboss"}
                    or Path(plugin.path).name.casefold() in {"ausboss-nodes", "comfyui-ausboss"}
                    or (plugin.repository or "").rstrip("/").removesuffix(".git").casefold()
                    == "https://github.com/ausboss/comfyui-ausboss"
                    for plugin in installed
                ):
                    await cf.comfyui.stop()
                    await cf.plugins.install_git("https://github.com/ausboss/ComfyUI-AusBoss")
            info = await cf.comfyui.start()
            required = {"AUSBOSS_NODES_TiledUpscale", "AUSBOSS_NODES_TiledUpscaleStitch",
                        "TextEncodeQwenImage21", "QwenImage21Cache"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Missing backend nodes: {sorted(missing)}")
            stages.append({"phase": "connect_start", "seconds": time.perf_counter() - started})
            if args.download_models:
                for category, repository, path in WEIGHTS[:4] if args.no_turbo else WEIGHTS:
                    started = time.perf_counter()
                    source = await cf.models.resolve({"repo_id": repository, "path": path})
                    artifact, = source.files
                    filename = Path(path).name
                    file = await cf.models.download(artifact, category=category, path=filename, reuse=True)
                    stages.append({"phase": "model_download", "file": path,
                                   "sha256": artifact.sha256, "revision": source.revision,
                                   "root": file.root.path, "priority": file.root.priority,
                                   "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            uploaded = await cf.files.upload(args.input)
            stages.append({"phase": "upload", "seconds": time.perf_counter() - started})
            try:
                started = time.perf_counter()
                image, _ = cf.node("LoadImage", image=uploaded.native_name)
                clip = cf.node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default")
                vae = cf.node("VAELoader", vae_name=VAE)
                model = cf.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
                model = cf.node("LoraLoaderModelOnly", model=model, lora_name=RESTORE, strength_model=1.0)
                if not args.no_turbo:
                    model = cf.node("LoraLoaderModelOnly", model=model, lora_name=TURBO, strength_model=1.0)
                model = cf.node("QwenImage21Cache", model=model, device="auto", dtype="default")
                await asyncio.gather(image, clip, vae, model)
                stages.append({"phase": "load_objects", "seconds": time.perf_counter() - started})
                started = time.perf_counter()
                tiles, stitcher, before, _width, _height, plan = cf.node(
                    "AUSBOSS_NODES_TiledUpscale", image=image, megapixels=args.megapixels,
                    tile_megapixels=args.tile_megapixels, overlap=128, multiple=32, max_growth=8.0,
                )
                profile["tile_plan"] = await (await plan).fetch()
                # Native OUTPUT_IS_LIST maps each tile through these nodes in the original order.
                positive, negative, latent = cf.node(
                    "TextEncodeQwenImage21", clip=clip, vae=vae, prompt=args.prompt,
                    negative_prompt="", resolution=0, **{"images.image_1": tiles},
                )
                await asyncio.gather(positive, negative, latent)
                stages.append({"phase": "tile_encode", "seconds": time.perf_counter() - started})
                for index in range(args.count):
                    seed = args.seed + index
                    started = time.perf_counter()
                    samples = cf.node("KSampler", model=model, positive=positive, negative=negative,
                                          latent_image=latent, seed=seed, steps=args.steps, cfg=1.0,
                                          sampler_name="euler", scheduler="simple", denoise=1.0)
                    await samples
                    stages.append({"phase": "sample_tiles", "seed": seed, "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    redrawn = cf.node("VAEDecode", samples=samples, vae=vae)
                    await redrawn
                    stages.append({"phase": "decode_tiles", "seed": seed, "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    restored = cf.node("AUSBOSS_NODES_TiledUpscaleStitch", tiles=redrawn,
                                           stitcher=stitcher, keep_colors=1.0)
                    await restored
                    stages.append({"phase": "stitch", "seed": seed, "seconds": time.perf_counter() - started})
                    outputs = {"restored": restored}
                    if index == 0:
                        outputs["plain-upscale"] = before
                    for name, output in outputs.items():
                        started = time.perf_counter()
                        saved = cf.node("SaveImage", images=output,
                                            filename_prefix=f"comfier/tiled/{name}", return_call=True)
                        file = await saved.file()
                        stages.append({"phase": "save", "name": name,
                                       "seconds": time.perf_counter() - started})
                        started = time.perf_counter()
                        target = args.output_dir / f"seed{seed}-{name}.png"
                        await cf.files.download(file, target)
                        stages.append({"phase": "download", "name": name, "bytes": file.size,
                                       "seconds": time.perf_counter() - started})
                        print(target, flush=True)
            finally:
                await cf.files.remove(uploaded)
    except Exception as error:
        profile["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        profile["total_seconds"] = time.perf_counter() - total_started
        (args.output_dir / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/qwen-tiled-upscale"))
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--megapixels", type=float, default=4.0)
    parser.add_argument("--tile-megapixels", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--count", type=int, default=1, help="new products at seed, seed+1, ...; models stay loaded")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--no-turbo", action="store_true")
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--install-plugin", action="store_true",
                        help="install AusBoss if missing; only then stop the runtime")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    asyncio.run(main(args))
