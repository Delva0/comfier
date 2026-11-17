"""Point at an object, repaint it with Qwen 2.1, and preserve the other pixels.

Independent eager adaptation of AusBoss's Click to Mask workflow:
https://civitai.com/models/3000390 (AusBoss, v1.1).
Requires current ComfyUI, ComfyUI-AusBoss, Qwen Image 2.1 and SAM 3.1.
Models retain their own licenses; Qwen 2.1 is for noncommercial research.

python examples/qwen_click_mask.py --input desk.png --point 400 300 \
    --prompt "Replace the cup with a glass of orange juice." --editable --output-dir artifacts/click
Add --download-models to acquire the four weights through the daemon.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
--editable adds this example's independent RGBA patch, two-layer OpenRaster
project, blend/sampling masks and source/geometry manifest. ORA alpha is baked into
a paint layer; native editor import and arbitrary editor masks are not verified.
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

import comfier as cf

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


def editable_patch(source, edited, blend, sampling_mask, crop, stitcher, source_sha256):
    """Run remotely: recover unassociated edit color before PNG quantization.

    The old stitcher's flattened image is a preview. An independent patch can
    instead be placed over a later base after an author reviews its context.
    """
    import hashlib
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    import numpy as np
    import torch
    from PIL import Image

    if source.shape != edited.shape or source.shape[0] != 1 or source.shape[-1] != 3:
        raise ValueError("Editable delivery requires one RGB source and matching stitched image")
    alpha = blend.unsqueeze(-1).to(source)
    color = torch.where(alpha > 0, (edited - source * (1 - alpha)) / alpha.clamp_min(1e-12),
                        torch.zeros_like(source)).clamp(0, 1)
    reconstructed = source * (1 - alpha) + color * alpha
    error = float((reconstructed - edited).abs().max())
    if error > 1e-5:
        raise ValueError(f"Independent patch does not reconstruct the stitch preview: {error}")
    pixels = torch.nonzero(blend[0] > 0)
    y, x = pixels.min(0).values.tolist() if len(pixels) else (0, 0)
    bottom, right = (pixels.max(0).values + 1).tolist() if len(pixels) else (1, 1)

    def image(tensor):
        return Image.fromarray(np.rint(tensor.detach().cpu().numpy().clip(0, 1) * 255).astype(np.uint8))

    def png(value):
        data = io.BytesIO()
        value.save(data, format="PNG")
        return data.getvalue()

    base = image(source[0]).convert("RGBA")
    patch = image(torch.cat((color, alpha), -1)[0, y:bottom, x:right])
    layer = Image.new("RGBA", base.size)
    layer.paste(patch, (x, y))
    merged = Image.alpha_composite(base, layer)
    files = {"source.png": png(base), "patch.png": png(patch),
             "blend-mask.png": png(image(blend[0])),
             "sampling-mask.png": png(image(sampling_mask[0]))}
    height, width = source.shape[1:3]
    root = ET.Element("image", w=str(width), h=str(height), version="0.0.3", name="Qwen local edit")
    stack = ET.SubElement(root, "stack")
    for name, src, px, py in (("Independent edit", "patch", x, y), ("Source revision", "source", 0, 0)):
        ET.SubElement(stack, "layer", {"name": name, "src": f"data/{src}.png", "x": str(px),
                      "y": str(py), "opacity": "1.0", "visibility": "visible", "composite-op": "svg:src-over"})
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as ora:
        ora.writestr("mimetype", "image/openraster", compress_type=zipfile.ZIP_STORED)
        ora.writestr("stack.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        for name in ("source", "patch"):
            ora.writestr(f"data/{name}.png", files[f"{name}.png"])
        ora.writestr("mergedimage.png", png(merged))
        thumbnail = merged.copy()
        thumbnail.thumbnail((256, 256))
        ora.writestr("Thumbnails/thumbnail.png", png(thumbnail))
    files["editable.ora"] = archive.getvalue()
    manifest = {"schema": 1, "source_file_sha256": source_sha256,
                "source_pixels_sha256": hashlib.sha256(source.detach().cpu().numpy().tobytes()).hexdigest(),
                "crop_pixels_sha256": hashlib.sha256(crop.detach().cpu().numpy().tobytes()).hexdigest(),
                "canvas_size": [width, height], "patch_xywh": [x, y, right - x, bottom - y],
                "crop_transform": {key: stitcher[key] for key in
                    ("canvas_to_original", "crop_to_canvas", "scale", "sampler_size", "algorithm")},
                "sampling_mask": "sampler coordinates; controls generation, not paste alpha",
                "blend_mask": "source canvas coordinates; patch alpha before 8-bit quantization",
                "float_reconstruction_max_abs": error,
                "ora": {"layers": 2, "alpha": "baked into patch paint layer", "editor_import_verified": False},
                "rebase": "Review canvas geometry and generation context before placing on a changed base.",
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    return {"files": files, "manifest": manifest}


async def acquire_models(report):
    for category, repository, path in WEIGHTS:
        started = time.perf_counter()
        source = await cf.models.resolve({"repo_id": repository, "path": path})
        artifact, = source.files
        filename = Path(path).name
        file = await cf.models.download(artifact, category=category, path=filename, reuse=True)
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
        async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
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
            uploaded = await cf.files.upload(args.input)
            phases.append({"phase": "upload", "seconds": time.perf_counter() - started})
            try:
                started = time.perf_counter()
                image, _ = cf.node("LoadImage", image=uploaded.native_name)
                clip = cf.node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default")
                vae = cf.node("VAELoader", vae_name=VAE)
                sam, sam_clip, _ = cf.node("CheckpointLoaderSimple", ckpt_name=SAM)
                model = cf.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
                model = cf.node("QwenImage21Cache", model=model, device="auto", dtype="default")
                await asyncio.gather(image, clip, vae, sam, sam_clip, model)
                phases.append({"phase": "load_objects", "seconds": time.perf_counter() - started,
                               "note": "Loader objects; GPU movement also occurs during encode/sample."})
                # Pixel coordinates refer to the uploaded image, without a browser or hover request.
                touches = json.dumps({"v": 1, "items": [
                    {"t": "tap", "op": 1, "x": x, "y": y} for x, y in args.point]})
                # Keep these Futures for this fixed input/selection/prompt. A new
                # invocation prepares them again when any of those inputs change.
                started = time.perf_counter()
                mask, _cutout, _names = cf.node(
                    "AUSBOSS_NODES_MaskByTouch", image=image, model=sam, clip=sam_clip,
                    touches=touches, grow=16, soften=0.0, smooth=0, fill_holes=True,
                    if_nothing="stop the run", free_vram=True, invert=False, ask="", fine_edges="off",
                )
                await mask
                phases.append({"phase": "prepare_segment", "seconds": time.perf_counter() - started})
                started = time.perf_counter()
                crop, crop_mask, stitcher = cf.node(
                    "AUSBOSS_NODES_CropForInpaint", image=image, mask=mask, context_factor=1.6,
                    blend_pixels=8, output_multiple=32, target_megapixels=args.megapixels,
                    rescale_algorithm="bicubic", keep_inside=True,
                )
                sample_mask = cf.node("GrowMask", mask=crop_mask, expand=8, tapered_corners=True)
                positive, negative, _ = cf.node(
                    "TextEncodeQwenImage21", clip=clip, vae=vae,
                    prompt=args.prompt + " Keep everything else exactly the same.",
                    negative_prompt="", resolution=0, **{"images.image_1": crop},
                )
                latent = cf.node("VAEEncode", pixels=crop, vae=vae)
                latent = cf.node("SetLatentNoiseMask", samples=latent, mask=sample_mask)
                debug_inputs = {}
                if args.debug_images:
                    debug_inputs.update(source=image, crop=crop,
                                        selection=cf.node("MaskToImage", mask=mask),
                                        sample_mask=cf.node("MaskToImage", mask=sample_mask))
                await asyncio.gather(positive, negative, latent, *debug_inputs.values())
                phases.append({"phase": "prepare_crop_encode", "seconds": time.perf_counter() - started})
                for index in range(args.count * args.repeat):
                    seed = args.seed + index // args.repeat
                    run_started = time.perf_counter()
                    run = {"index": index + 1, "seed": seed, "cache_repeat": index % args.repeat + 1}
                    report["runs"].append(run)
                    sampled = cf.node(
                        "KSampler", model=model, positive=positive, negative=negative,
                        latent_image=latent, seed=seed, steps=args.steps, cfg=1.0,
                        sampler_name="euler", scheduler="simple", denoise=1.0,
                    )
                    painted = cf.node("VAEDecode", samples=sampled, vae=vae)
                    edited, blend = cf.node("AUSBOSS_NODES_StitchInpaint", stitcher=stitcher,
                                                inpainted=painted, fix_edge_halo=False, color_match=0.0)
                    if args.editable:
                        bundle = await (await cf.python(source=Path(__file__), entry="editable_patch", inputs={
                            "source": image, "edited": edited, "blend": blend, "sampling_mask": sample_mask,
                            "crop": crop, "stitcher": stitcher, "source_sha256": uploaded.sha256,
                        })).fetch()
                        stem = f"seed{seed}-repeat{index % args.repeat + 1:02d}"
                        for name, data in bundle["files"].items():
                            (args.output_dir / f"{stem}-{name}").write_bytes(data)
                        manifest = bundle["manifest"]
                        manifest.update(seed=seed, prompt=args.prompt,
                                        files={name: f"{stem}-{name}" for name in bundle["files"]})
                        (args.output_dir / f"{stem}-manifest.json").write_text(json.dumps(manifest, indent=2))
                        print(args.output_dir / f"{stem}-editable.ora", flush=True)
                        run["editable"] = manifest
                    outputs = {"edited": edited}
                    if args.debug_images:
                        outputs.update(debug_inputs)
                        outputs["blend_mask"] = cf.node("MaskToImage", mask=blend)
                    for name, output in outputs.items():
                        saved = cf.node("SaveImage", images=output,
                                            filename_prefix=f"comfier/click/{name}", return_call=True)
                        file = await saved.file()
                        target = args.output_dir / f"seed{seed}-repeat{index % args.repeat + 1:02d}-{name}.png"
                        await cf.files.download(file, target)
                        print(target, flush=True)
                    run["wall_seconds"] = time.perf_counter() - run_started
                    run["cache_note"] = ("Fixed input/mask/conditioning prepared once; --repeat repeats this seed's "
                                         "sampling and downstream work, including native cache reuse.")
            finally:
                await cf.files.remove(uploaded)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (args.output_dir / "profile.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
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
    parser.add_argument("--editable", action="store_true",
                        help="also export independent RGBA patch, ORA, masks and source/geometry manifest")
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--install-plugin", action="store_true", help="install AusBoss if missing; only then stop the runtime")
    args = parser.parse_args()
    if args.count < 1 or args.repeat < 1:
        parser.error("--count and --repeat must be positive")
    asyncio.run(main(args))
