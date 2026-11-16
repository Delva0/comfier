"""Extract an object as RGBA PNG and fill its old position in a second PNG.

Independent eager adaptation of AusBoss's Cut Out + Remove v1.0:
https://civitai.com/models/2987326 . Needs current ComfyUI + ComfyUI-AusBoss.
Qwen Image 2.1 weights and the consistency LoRA retain their research licenses.

python examples/qwen_cut_out_remove.py --input desk.png --object "the mug" \
    --output-dir artifacts/cutout --debug-images
Add --download-models to acquire the five weights through the daemon.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
The desk-photo diagnostic used --change-threshold 0.08 --no-alpha-fill-holes
--alpha-expand -2 --alpha-white-point 0.999. These are material-dependent alpha
and edit-mask adjustments; they cannot guarantee perfect matting for every image.
The selection, foreground, conditioning, latent and fixed composite inputs are
prepared once. --count changes seeds; --repeat repeats sampling/downstream work.
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
LORA = "qwen-image-2.1-consistency.safetensors"
WEIGHTS = (
    ("diffusion_models", "Comfy-Org/Qwen-Image-2.1", "diffusion_models/" + MODEL),
    ("text_encoders", "Comfy-Org/Qwen-Image-2.1", "text_encoders/" + CLIP),
    ("vae", "Comfy-Org/Qwen-Image-2.1", "vae/" + VAE),
    ("checkpoints", "Comfy-Org/sam3.1", "checkpoints/" + SAM),
    ("loras", "ausboss/Qwen-Image-2.1-Consistency-LoRA", LORA),
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
    report = {"workflow": "https://civitai.com/models/2987326", "arguments": {
        key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "phases": [], "runs": []}
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
            required = {"SAM3_Detect", "AUSBOSS_NODES_ImageResize", "AUSBOSS_NODES_RefineMask",
                        "TextEncodeQwenImage21", "QwenImage21Cache", "ImageRGBToYUV"}
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
                original, _ = comani.node("LoadImage", image=uploaded.native_name)
                image, _, _, _ = comani.node(
                    "AUSBOSS_NODES_ImageResize", image=original, target_mode="megapixels",
                    width=0, height=0, edge_length=1024, megapixels=args.megapixels, scale_factor=0.5,
                    keep_proportion="fit", fill_color="#000000", divisible_by=32, interpolation="lanczos",
                )
                clip = comani.node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default")
                vae = comani.node("VAELoader", vae_name=VAE)
                sam, sam_clip, _ = comani.node("CheckpointLoaderSimple", ckpt_name=SAM)
                model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
                model = comani.node("LoraLoaderModelOnly", model=model, lora_name=LORA, strength_model=1.0)
                model = comani.node("QwenImage21Cache", model=model, device="auto", dtype="default")
                await asyncio.gather(image, clip, vae, sam, sam_clip, model)
                phases.append({"phase": "load_objects_resize", "seconds": time.perf_counter() - started,
                               "note": "Loader objects; GPU movement also occurs during encode/sample."})
                prompt = (f"Take {args.object} out of the picture completely, along with anything worn, "
                          f"held or carried, and the shadow and reflection left behind. No part of "
                          f"{args.object} may remain. Fill the space with {args.fill}. "
                          "Keep everything else exactly the same.")
                # Explicitly reuse only values independent of the sampling seed.
                # Each invocation rebuilds them for its own input/object/prompt.
                started = time.perf_counter()
                find = comani.node("CLIPTextEncode", clip=sam_clip, text=args.object)
                selection, _ = comani.node("SAM3_Detect", model=sam, image=image, conditioning=find,
                                           threshold=0.5, refine_iterations=2, individual_masks=False)
                alpha, alpha_inverse = comani.node(
                    "AUSBOSS_NODES_RefineMask", mask=selection, expand=args.alpha_expand, blur=1.0,
                    fill_holes=args.alpha_fill_holes, smooth=3, black_point=0.0,
                    white_point=args.alpha_white_point,
                    edge_refine="off", preview=False,
                )
                _, outside = comani.node(
                    "AUSBOSS_NODES_RefineMask", mask=selection, expand=8, blur=0.0,
                    fill_holes=True, smooth=3, black_point=0.0, white_point=1.0,
                    edge_refine="off", preview=False,
                )
                black = comani.node("EmptyImage", width=64, height=64, batch_size=1, color=0)
                foreground = comani.node("ImageCompositeMasked", destination=image, source=black,
                                         mask=outside, x=0, y=0, resize_source=True)
                # JoinImageWithAlpha uses ComfyUI's MASK convention: alpha=1 means transparent.
                foreground = comani.node("JoinImageWithAlpha", image=foreground, alpha=alpha_inverse)
                debug_masks = {}
                if args.debug_images:
                    debug_masks.update(selection=comani.node("MaskToImage", mask=selection),
                                       alpha=comani.node("MaskToImage", mask=alpha))
                await asyncio.gather(foreground, *debug_masks.values())
                phases.append({"phase": "prepare_segment_cutout", "seconds": time.perf_counter() - started})
                started = time.perf_counter()
                positive, negative, latent = comani.node(
                    "TextEncodeQwenImage21", clip=clip, vae=vae, prompt=prompt,
                    negative_prompt="", resolution=0, **{"images.image_1": image},
                )
                blurred_source = comani.node("ImageBlur", image=image, blur_radius=15, sigma=5.0)
                object_area, _ = comani.node(
                    "AUSBOSS_NODES_RefineMask", mask=selection, expand=40, blur=16.0,
                    fill_holes=True, smooth=0, black_point=0.0, white_point=1.0,
                    edge_refine="off", preview=False,
                )
                await asyncio.gather(positive, negative, latent, blurred_source, object_area)
                phases.append({"phase": "prepare_encode_composite", "seconds": time.perf_counter() - started})
                for index in range(args.count * args.repeat):
                    seed = args.seed + index // args.repeat
                    run_started = time.perf_counter()
                    run = {"index": index + 1, "seed": seed, "cache_repeat": index % args.repeat + 1, "phases": []}
                    report["runs"].append(run)
                    stages = run["phases"]
                    started = time.perf_counter()
                    sampled = comani.node(
                        "KSampler", model=model, positive=positive, negative=negative, latent_image=latent,
                        seed=seed, steps=args.steps, cfg=1.0, sampler_name="euler", scheduler="simple",
                        denoise=1.0,
                    )
                    await sampled
                    stages.append({"phase": "sample", "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    generated = comani.node("VAEDecode", samples=sampled, vae=vae)
                    generated, _ = comani.node("SplitImageWithAlpha", image=generated)
                    await generated
                    stages.append({"phase": "decode", "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    # The original workflow also includes shadows/reflections in its change mask.
                    blurred_edit = comani.node("ImageBlur", image=generated, blur_radius=15, sigma=5.0)
                    difference1 = comani.node("ImageBlend", image1=blurred_source, image2=blurred_edit,
                                              blend_factor=1.0, blend_mode="difference")
                    difference2 = comani.node("ImageBlend", image1=blurred_edit, image2=blurred_source,
                                              blend_factor=1.0, blend_mode="difference")
                    difference = comani.node("ImageBlend", image1=difference1, image2=difference2,
                                             blend_factor=1.0, blend_mode="screen")
                    luminance, _, _ = comani.node("ImageRGBToYUV", image=difference)
                    changes = comani.node("ImageToMask", image=luminance, channel="red")
                    changes = comani.node("ThresholdMask", mask=changes, value=args.change_threshold)
                    changed_area, _ = comani.node(
                        "AUSBOSS_NODES_RefineMask", mask=changes, expand=24, blur=12.0,
                        fill_holes=True, smooth=0, black_point=0.0, white_point=1.0,
                        edge_refine="off", preview=False,
                    )
                    edit_mask = comani.node("MaskComposite", destination=changed_area, source=object_area,
                                           x=0, y=0, operation="add")
                    background = comani.node("ImageCompositeMasked", destination=image, source=generated,
                                             mask=edit_mask, x=0, y=0, resize_source=False)
                    await background
                    stages.append({"phase": "keep_rest", "seconds": time.perf_counter() - started})
                    outputs = {"foreground": foreground, "background": background}
                    if args.debug_images:
                        outputs.update(source=image, generated=generated)
                        outputs.update(debug_masks)
                        outputs["edit_mask"] = comani.node("MaskToImage", mask=edit_mask)
                    for name, output in outputs.items():
                        started = time.perf_counter()
                        saved = comani.node("SaveImage", images=output,
                                            filename_prefix=f"comani/cutout/{name}", return_call=True)
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
    parser.add_argument("--object", required=True)
    parser.add_argument("--fill", default="the background that would be behind")
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--megapixels", type=float, default=1.5)
    parser.add_argument("--change-threshold", type=float, default=0.03,
                        help="background difference threshold; source default0.03")
    parser.add_argument("--alpha-fill-holes", action=argparse.BooleanOptionalAction, default=True,
                        help="fill foreground alpha holes; disable to preserve holes such as handles")
    parser.add_argument("--alpha-expand", type=int, default=0,
                        help="foreground alpha expansion in pixels; negative values contract")
    parser.add_argument("--alpha-white-point", type=float, default=1.0,
                        help="foreground alpha white point; source default1.0")
    parser.add_argument("--count", type=int, default=1, help="new products at seed, seed+1, ...; models stay loaded")
    parser.add_argument("--repeat", type=int, default=1,
                        help="same-seed sampling/downstream cache diagnostic; fixed preparation runs once")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/qwen-cutout"))
    parser.add_argument("--debug-images", action="store_true")
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--install-plugin", action="store_true",
                        help="install AusBoss if missing; only then stop the runtime")
    args = parser.parse_args()
    if not 0.0 <= args.change_threshold <= 1.0 or not 0.0 < args.alpha_white_point <= 1.0:
        parser.error("threshold must be in[0,1] and alpha white point in(0,1]")
    if args.count < 1 or args.repeat < 1:
        parser.error("--count and --repeat must be positive")
    asyncio.run(main(args))
