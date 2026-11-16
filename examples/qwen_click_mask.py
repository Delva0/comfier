"""Point at an object, repaint it with Qwen 2.1, and preserve the other pixels.

Independent eager adaptation of AusBoss's Click to Mask workflow:
https://civitai.com/models/3000390 (AusBoss, v1.1).
Requires current ComfyUI, ComfyUI-AusBoss, Qwen Image 2.1 and SAM 3.1.
Models retain their own licenses; Qwen 2.1 is for noncommercial research.

python examples/qwen_click_mask.py --input desk.png --point 400 300 \
    --prompt "Replace the cup with a glass of orange juice." --output-dir artifacts/click
Add --download-models to acquire the four weights through the daemon.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
The input, selection, crop, conditioning and masked latent are prepared once.
--count changes seeds; --repeat repeats sampling and downstream work for a seed.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import comani

MODEL = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
SAM = "sam3.1_multiplex_fp16.safetensors"
WEIGHTS = (
    ("diffusion_models", "Comfy-Org/Qwen-Image-2.1", "diffusion_models/" + MODEL),
    ("text_encoders", "Comfy-Org/Qwen-Image-2.1", "text_encoders/" + CLIP),
    ("vae", "Comfy-Org/Qwen-Image-2.1", "vae/" + VAE),
    ("checkpoints", "Comfy-Org/sam3.1", "checkpoints/" + SAM),
)


async def acquire_models(report):
    for category, repository, path in WEIGHTS:
        started = time.perf_counter()
        source = await comani.models.resolve({"repo_id": repository, "path": path})
        artifact, = source.files
        filename = Path(path).name
        file = await comani.models.download(artifact, category=category, path=filename, reuse=True)
        report.append({"phase": "model_download", "file": path, "revision": source.revision,
                       "sha256": artifact.sha256, "root": file.root.path, "priority": file.root.priority,
                       "seconds": time.perf_counter() - started})


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass  # Check file creation before starting remote work.
    print(f"Output directory: {args.output_dir}", flush=True)
    report = {"workflow": "https://civitai.com/models/3000390", "arguments": vars(args).copy(),
              "phases": [], "runs": []}
    report["arguments"] = {key: str(value) if isinstance(value, Path) else value
                           for key, value in report["arguments"].items()}
    phases = report["phases"]
    started = time.perf_counter()
    try:
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            if args.install_plugin:
                installed = await comani.plugins.list()
                if not any(
                    plugin.id.casefold() in {"ausboss-nodes", "comfyui-ausboss"}
                    or Path(plugin.path).name.casefold() in {"ausboss-nodes", "comfyui-ausboss"}
                    or (plugin.repository or "").rstrip("/").removesuffix(".git").casefold()
                    == "https://github.com/ausboss/comfyui-ausboss"
                    for plugin in installed
                ):
                    await comani.comfyui.stop()
                    await comani.plugins.install_git("https://github.com/ausboss/ComfyUI-AusBoss")
            info = await comani.comfyui.start()
            required = {"AUSBOSS_NODES_MaskByTouch", "AUSBOSS_NODES_CropForInpaint",
                        "AUSBOSS_NODES_StitchInpaint", "TextEncodeQwenImage21", "QwenImage21Cache"}
            available = info.extra.get("node_metadata", {}).get("nodes", {})
            if missing := required - available.keys():
                raise ValueError(f"Missing backend nodes: {sorted(missing)}")
            phases.append({"phase": "connect_start", "seconds": time.perf_counter() - started})
            report["comfyui"] = {"root": info.comfy_root, "runtime_python": info.runtime_python,
                                 "runtime_epoch": info.runtime_epoch}
            if args.download_models:
                await acquire_models(phases)
            started = time.perf_counter()
            uploaded = await comani.files.upload(args.input)
            phases.append({"phase": "upload", "seconds": time.perf_counter() - started})
            try:
                started = time.perf_counter()
                image, _ = comani.node("LoadImage", image=uploaded.native_name)
                clip = comani.node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default")
                vae = comani.node("VAELoader", vae_name=VAE)
                sam, sam_clip, _ = comani.node("CheckpointLoaderSimple", ckpt_name=SAM)
                model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
                model = comani.node("QwenImage21Cache", model=model, device="auto", dtype="default")
                await asyncio.gather(image, clip, vae, sam, sam_clip, model)
                phases.append({"phase": "load_objects", "seconds": time.perf_counter() - started,
                               "note": "Loader objects; GPU movement also occurs during encode/sample."})
                # Pixel coordinates refer to the uploaded image, without a browser or hover request.
                touches = json.dumps({"v": 1, "items": [
                    {"t": "tap", "op": 1, "x": x, "y": y} for x, y in args.point]})
                # Keep these Futures for this fixed input/selection/prompt. A new
                # invocation prepares them again when any of those inputs change.
                started = time.perf_counter()
                mask, _cutout, _names = comani.node(
                    "AUSBOSS_NODES_MaskByTouch", image=image, model=sam, clip=sam_clip,
                    touches=touches, grow=16, soften=0.0, smooth=0, fill_holes=True,
                    if_nothing="stop the run", free_vram=True, invert=False, ask="", fine_edges="off",
                )
                await mask
                phases.append({"phase": "prepare_segment", "seconds": time.perf_counter() - started})
                started = time.perf_counter()
                crop, crop_mask, stitcher = comani.node(
                    "AUSBOSS_NODES_CropForInpaint", image=image, mask=mask, context_factor=1.6,
                    blend_pixels=8, output_multiple=32, target_megapixels=args.megapixels,
                    rescale_algorithm="bicubic", keep_inside=True,
                )
                sample_mask = comani.node("GrowMask", mask=crop_mask, expand=8, tapered_corners=True)
                positive, negative, _ = comani.node(
                    "TextEncodeQwenImage21", clip=clip, vae=vae,
                    prompt=args.prompt + " Keep everything else exactly the same.",
                    negative_prompt="", resolution=0, **{"images.image_1": crop},
                )
                latent = comani.node("VAEEncode", pixels=crop, vae=vae)
                latent = comani.node("SetLatentNoiseMask", samples=latent, mask=sample_mask)
                debug_inputs = {}
                if args.debug_images:
                    debug_inputs.update(source=image, crop=crop,
                                        selection=comani.node("MaskToImage", mask=mask),
                                        sample_mask=comani.node("MaskToImage", mask=sample_mask))
                await asyncio.gather(positive, negative, latent, *debug_inputs.values())
                phases.append({"phase": "prepare_crop_encode", "seconds": time.perf_counter() - started})
                for index in range(args.count * args.repeat):
                    seed = args.seed + index // args.repeat
                    run_started = time.perf_counter()
                    run = {"index": index + 1, "seed": seed, "cache_repeat": index % args.repeat + 1, "phases": []}
                    report["runs"].append(run)
                    stages = run["phases"]
                    started = time.perf_counter()
                    sampled = comani.node(
                        "KSampler", model=model, positive=positive, negative=negative,
                        latent_image=latent, seed=seed, steps=args.steps, cfg=1.0,
                        sampler_name="euler", scheduler="simple", denoise=1.0,
                    )
                    await sampled
                    stages.append({"phase": "sample", "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    painted = comani.node("VAEDecode", samples=sampled, vae=vae)
                    await painted
                    stages.append({"phase": "decode", "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    edited, blend = comani.node("AUSBOSS_NODES_StitchInpaint", stitcher=stitcher,
                                                inpainted=painted, fix_edge_halo=False, color_match=0.0)
                    await edited
                    stages.append({"phase": "stitch", "seconds": time.perf_counter() - started})
                    outputs = {"edited": edited}
                    if args.debug_images:
                        outputs.update(debug_inputs)
                        outputs["blend_mask"] = comani.node("MaskToImage", mask=blend)
                    for name, output in outputs.items():
                        started = time.perf_counter()
                        saved = comani.node("SaveImage", images=output,
                                            filename_prefix=f"comani/click/{name}", return_call=True)
                        file = await saved.file()
                        stages.append({"phase": "save", "name": name,
                                       "seconds": time.perf_counter() - started})
                        started = time.perf_counter()
                        target = args.output_dir / f"seed{seed}-repeat{index % args.repeat + 1:02d}-{name}.png"
                        await comani.files.download(file, target)
                        stages.append({"phase": "download", "name": name, "bytes": file.size,
                                       "seconds": time.perf_counter() - started})
                        print(target, flush=True)
                    run["wall_seconds"] = time.perf_counter() - run_started
                    run["cache_note"] = ("Fixed input/mask/conditioning prepared once; --repeat repeats this seed's "
                                         "sampling and downstream work, including native cache reuse.")
            finally:
                await comani.files.remove(uploaded)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (args.output_dir / "profile.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--point", type=int, nargs=2, action="append", required=True, metavar=("X", "Y"))
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--megapixels", type=float, default=1.5)
    parser.add_argument("--count", type=int, default=1, help="new products at seed, seed+1, ...; models stay loaded")
    parser.add_argument("--repeat", type=int, default=1,
                        help="same-seed sampling/downstream cache diagnostic; fixed preparation runs once")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/qwen-click-mask"))
    parser.add_argument("--debug-images", action="store_true")
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--install-plugin", action="store_true", help="install AusBoss if missing; only then stop the runtime")
    args = parser.parse_args()
    if args.count < 1 or args.repeat < 1:
        parser.error("--count and --repeat must be positive")
    asyncio.run(main(args))
