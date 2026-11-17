"""Place two characters in explicit boxes, then optionally refine the image.

Independent eager adaptation of HissingCat's MOSS BBOX V3 Hires workflow
(resource3005854/version3442452). Its optional second pass is off in the
supplied GUI; --hires enables that native branch. Model/LoRA cards: Apache2.0.
The browser-only bbox_aspect extension is unnecessary for explicit pixel boxes.

python examples/moss_bbox.py --download-models --output-dir artifacts/moss-bbox
--boxes accepts the core CreateBoundingBoxes pixel-box JSON, with metadata.desc.
Each invocation creates a new run-* subdirectory under the output root.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import comfier as cf

MODEL = "moss_image_sft_int8_convrot.safetensors"
CLIP = "qwen3.5_9b_int8_convrot.safetensors"
VAE = "flux2-vae_small.safetensors"
LORA = "moss_image_sft_8steps_dmd.safetensors"
WEIGHTS = (
    ("diffusion_models", "Comfy-Org/MOSS-Image", "diffusion_models/" + MODEL, MODEL),
    ("text_encoders", "Comfy-Org/MOSS-Image", "text_encoders/" + CLIP, CLIP),
    ("vae", "Comfy-Org/MOSS-Image", "vae/" + VAE, VAE),
    ("loras", "lodestones/MOSS-Image-SFT-8-steps-dmd", "lora.safetensors", LORA),
)
PROMPT = ("A full-length studio photograph of two fictional adult explorers standing side by side. "
          "The explorer on the left wears a red jacket and holds a folded map. "
          "The explorer on the right wears a blue jacket and carries a yellow backpack. "
          "Neutral light gray background, soft lighting, each person completely visible.")


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass
    print(f"Output directory: {args.output_dir}", flush=True)
    boxes = json.loads(args.boxes.read_text()) if args.boxes else [
        {"x": round(args.width * 0.06), "y": round(args.height * 0.12),
         "width": round(args.width * 0.39), "height": round(args.height * 0.82),
         "metadata": {"type": "obj", "desc": "Adult explorer in a red jacket holding a folded map",
                      "palette": ["#C52A31"]}},
        {"x": round(args.width * 0.55), "y": round(args.height * 0.12),
         "width": round(args.width * 0.39), "height": round(args.height * 0.82),
         "metadata": {"type": "obj", "desc": "Adult explorer in a blue jacket with a yellow backpack",
                      "palette": ["#2459BF", "#EAC446"]}},
    ]
    report = {"workflow_resource": 3005854, "workflow_version": 3442452, "boxes": boxes,
              "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              "phases": []}
    stages = report["phases"]
    try:
        async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
            started = time.perf_counter()
            info = await cf.comfyui.start()
            required = {"CreateBoundingBoxes", "MOSSImageLayoutPrompt", "SkipLayerGuidanceDiTSimple",
                        "EmptyFlux2LatentImage"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Missing native nodes: {sorted(missing)}")
            stages.append({"phase": "connect_start", "seconds": time.perf_counter() - started})
            if args.download_models:
                for category, repository, path, filename in WEIGHTS if args.hires else WEIGHTS[:3]:
                    started = time.perf_counter()
                    source = await cf.models.resolve({"repo_id": repository, "path": path})
                    artifact, = source.files
                    await cf.models.download(artifact, category=category, path=filename, reuse=True)
                    stages.append({"phase": "model_download", "file": filename, "sha256": artifact.sha256,
                                   "revision": source.revision, "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            clip = cf.node("CLIPLoader", clip_name=CLIP, type="moss_image", device="default")
            vae = cf.node("VAELoader", vae_name=VAE)
            base_model = cf.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
            model = cf.node("SkipLayerGuidanceDiTSimple", model=base_model, double_layers="",
                                single_layers="22", start_percent=0.0, end_percent=0.5)
            preview, _bounds, elements = cf.node("CreateBoundingBoxes", width=args.width,
                                                    height=args.height, editor_state=[], bboxes=json.dumps(boxes))
            layout = cf.node("MOSSImageLayoutPrompt", prompt=args.prompt, element=elements,
                                 width=args.width, height=args.height, character_boxes=args.character_boxes)
            positive = cf.node("CLIPTextEncode", clip=clip, text=layout)
            negative = cf.node("CLIPTextEncode", clip=clip, text=args.negative)
            latent = cf.node("EmptyFlux2LatentImage", width=args.width, height=args.height, batch_size=1)
            if args.hires:
                hires_model = cf.node("LoraLoaderModelOnly", model=model, lora_name=LORA, strength_model=0.9)
            await asyncio.gather(model, positive, negative, latent)
            stages.append({"phase": "load_layout_encode", "seconds": time.perf_counter() - started})
            (args.output_dir / "layout-prompt.json").write_text(await (await layout).fetch(), encoding="utf-8")
            for index in range(args.count):
                seed = args.seed + index
                started = time.perf_counter()
                samples = cf.node("KSampler", model=model, positive=positive, negative=negative,
                                      latent_image=latent, seed=seed, steps=args.steps, cfg=3.0,
                                      sampler_name="er_sde", scheduler="simple", denoise=1.0)
                image = cf.node("VAEDecode", samples=samples, vae=vae)
                await image
                stages.append({"phase": "sample_decode", "seed": seed, "seconds": time.perf_counter() - started})
                outputs = {"base": image}
                if index == 0:
                    outputs["boxes"] = preview
                if args.hires:
                    started = time.perf_counter()
                    enlarged = cf.node("ImageScaleBy", image=image, upscale_method="lanczos", scale_by=1.5)
                    encoded = cf.node("VAEEncode", pixels=enlarged, vae=vae)
                    refined = cf.node("KSampler", model=hires_model, positive=positive, negative=negative,
                                          latent_image=encoded, seed=args.hires_seed + index, steps=8, cfg=1.8,
                                          sampler_name="er_sde", scheduler="simple", denoise=0.4)
                    finished = cf.node("VAEDecode", samples=refined, vae=vae)
                    await finished
                    outputs["hires"] = finished
                    stages.append({"phase": "hires", "seed": seed, "seconds": time.perf_counter() - started})
                for name, output in outputs.items():
                    saved = cf.node("SaveImage", images=output,
                                        filename_prefix=f"comfier/moss/{name}", return_call=True)
                    file = await saved.file()
                    started = time.perf_counter()
                    target = args.output_dir / f"seed{seed}-{name}.png"
                    await cf.files.download(file, target)
                    stages.append({"phase": "download", "name": name, "bytes": file.size,
                                   "seconds": time.perf_counter() - started})
                    print(target, flush=True)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (args.output_dir / "profile.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--negative", default="")
    parser.add_argument("--boxes", type=Path, help="pixel-box list accepted by core CreateBoundingBoxes")
    parser.add_argument("--character-boxes", default="1,2")
    parser.add_argument("--width", type=int, default=832)
    parser.add_argument("--height", type=int, default=1248)
    parser.add_argument("--seed", type=int, default=400547982279317)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--hires", action="store_true", help="enable source's optional stage2 and its LoRA")
    parser.add_argument("--hires-seed", type=int, default=89811256741504)
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/moss-bbox"))
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    asyncio.run(main(args))
