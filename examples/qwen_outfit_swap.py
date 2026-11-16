"""Describe a garment photo, then dress the person in it using Qwen 2.1.

Eager adaptation of AusBoss v1.1: https://civitai.com/models/2983126 .
Needs current ComfyUI + ComfyUI-AusBoss and the Qwen 2.1 Outfit Swap LoRA.
The base model and LoRA retain the Qwen Research License.

python examples/qwen_outfit_swap.py --person adult.png --garment outfit.png \
    --output-dir artifacts/qwen-outfit
The same encoder checks whether feet/legs are visible and describes only the
clothing that fits that framing. --description supplies a manual description.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
"""

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path

import comani

MODEL = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
LORA = "qwen-image-2.1-outfit-swap.safetensors"
WEIGHTS = (
    ("diffusion_models", "Comfy-Org/Qwen-Image-2.1", "diffusion_models/" + MODEL),
    ("text_encoders", "Comfy-Org/Qwen-Image-2.1", "text_encoders/" + CLIP),
    ("vae", "Comfy-Org/Qwen-Image-2.1", "vae/" + VAE),
    ("loras", "ausboss/Qwen-Image-2.1-Outfit-Swap-Consistency-LoRA", LORA),
)
# The three vision instructions preserve the source workflow's visibility branches.
DESCRIPTION_START = (
    "You write short image-edit descriptions. Reply with one phrase only, no preamble, no quotes.\n\n"
    "Look at the clothing in this picture. "
)
DESCRIPTION_PROMPTS = {
    "full_body": DESCRIPTION_START + (
        "Describe the outfit in one short phrase for an image editor: name every garment, the top, "
        "the bottom (skirt, shorts, trousers, dress) and any shoes or accessories, each with its color, "
        "pattern and the details that identify it (collar, straps, sleeves, trim, buttons, belt, chain). "
        "Start with 'the' and end with 'shown in image 2'. Do not describe the background or any person."
    ),
    "no_feet": DESCRIPTION_START + (
        "Describe the outfit in one short phrase for an image editor: name every garment, the top, "
        "the bottom (skirt, shorts, trousers, dress) and any accessories, each with its color, pattern "
        "and the details that identify it (collar, straps, sleeves, trim, buttons, belt, chain). "
        "Leave out shoes, boots and socks: the picture the outfit goes on does not show the feet. "
        "Start with 'the' and end with 'shown in image 2, without the shoes'. "
        "Do not describe the background or any person."
    ),
    "upper_body": DESCRIPTION_START + (
        "The picture it goes on shows the person only from the chest or waist up, so describe only "
        "what is worn on the upper body, in one short phrase for an image editor: the outer layer "
        "(jacket, coat, cardigan, shirt, sweater, or the top of a dress or overalls) and what shows "
        "under it, each with its color, pattern and the details that identify it (collar, sleeves, "
        "buttons, trim, straps). Leave out trousers, skirts, shorts, the lower part of a dress, shoes "
        "and socks. Start with 'the' and end with 'shown in image 2'. "
        "Do not describe the background or any person."
    ),
}


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass  # Check file creation before starting remote work.
    print(f"Output directory: {args.output_dir}", flush=True)
    profile = {"workflow": "https://civitai.com/models/2983126", "phases": [], "arguments": {
        key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}}
    stages = profile["phases"]
    total_started = time.perf_counter()
    try:
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            started = time.perf_counter()
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
            required = {"TextEncodeQwenImage21", "QwenImage21Cache", "AUSBOSS_NODES_ImageResize"}
            if args.description is None:
                required.add("TextGenerate")
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Missing backend nodes: {sorted(missing)}")
            stages.append({"phase": "connect_start", "seconds": time.perf_counter() - started})
            if args.download_models:
                for category, repository, path in WEIGHTS:
                    started = time.perf_counter()
                    source = await comani.models.resolve({"repo_id": repository, "path": path})
                    artifact, = source.files
                    filename = Path(path).name
                    file = await comani.models.download(artifact, category=category, path=filename, reuse=True)
                    stages.append({"phase": "model_download", "file": path,
                                   "sha256": artifact.sha256, "revision": source.revision,
                                   "root": file.root.path, "priority": file.root.priority,
                                   "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            uploads = await asyncio.gather(comani.files.upload(args.person), comani.files.upload(args.garment))
            stages.append({"phase": "upload", "seconds": time.perf_counter() - started})
            try:
                started = time.perf_counter()
                person, _ = comani.node("LoadImage", image=uploads[0].native_name)
                garment, _ = comani.node("LoadImage", image=uploads[1].native_name)
                clip = comani.node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default")
                vae = comani.node("VAELoader", vae_name=VAE)
                model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
                model = comani.node("LoraLoaderModelOnly", model=model, lora_name=LORA, strength_model=1.0)
                model = comani.node("QwenImage21Cache", model=model, device="auto", dtype="default")
                await asyncio.gather(person, garment, clip, vae, model)
                stages.append({"phase": "load_objects", "seconds": time.perf_counter() - started})
                description = args.description
                if description is None:
                    started = time.perf_counter()
                    person_small, _, _, _ = comani.node(
                        "AUSBOSS_NODES_ImageResize", image=person, target_mode="megapixels",
                        width=0, height=0, edge_length=1024, megapixels=0.6, scale_factor=0.5,
                        keep_proportion="fit", fill_color="#000000", divisible_by=32, interpolation="lanczos",
                    )
                    garment_small, _, _, _ = comani.node(
                        "AUSBOSS_NODES_ImageResize", image=garment, target_mode="megapixels",
                        width=0, height=0, edge_length=1024, megapixels=0.6, scale_factor=0.5,
                        keep_proportion="fit", fill_color="#000000", divisible_by=32, interpolation="lanczos",
                    )
                    legs, _ = comani.node(
                        "TextGenerate", clip=clip, image=person_small,
                        prompt="Are the person's legs in this picture? Answer yes or no.",
                        max_length=16, sampling_mode="off", thinking=True, use_default_template=True, mtp="auto",
                    )
                    legs_text = await (await legs).fetch()
                    has_legs = bool(re.match(r"^\s*yes\b", legs_text, re.IGNORECASE))
                    feet_text = None
                    branch = "upper_body"
                    if has_legs:
                        feet, _ = comani.node(
                            "TextGenerate", clip=clip, image=person_small,
                            prompt="Are the person's feet or shoes in this picture? Answer yes or no.",
                            max_length=16, sampling_mode="off", thinking=True,
                            use_default_template=True, mtp="auto",
                        )
                        feet_text = await (await feet).fetch()
                        has_feet = bool(re.match(r"^\s*yes\b", feet_text, re.IGNORECASE))
                        branch = "full_body" if has_feet else "no_feet"
                    text, _ = comani.node(
                        "TextGenerate", clip=clip, image=garment_small, prompt=DESCRIPTION_PROMPTS[branch],
                        max_length=200, sampling_mode="off", thinking=True, use_default_template=True, mtp="auto",
                    )
                    description = re.sub(r'''^[\s"']+|[\s"'.]+$''', "", await (await text).fetch())
                    profile["visibility"] = {"feet": feet_text, "legs": legs_text, "branch": branch}
                    stages.append({"phase": "visibility_describe", "seconds": time.perf_counter() - started})
                prompt = (f"Dress {args.who} in image 1 in {description}. Keep their face, hair, hands, "
                          f"pose and the background exactly the same. {args.instruction}")
                profile.update(description=description, edit_prompt=prompt)
                print(prompt, flush=True)
                started = time.perf_counter()
                person, _, _, _ = comani.node(
                    "AUSBOSS_NODES_ImageResize", image=person, target_mode="megapixels",
                    width=0, height=0, edge_length=1024, megapixels=2.0, scale_factor=0.5,
                    keep_proportion="fit", fill_color="#000000", divisible_by=32, interpolation="lanczos",
                )
                garment, _, _, _ = comani.node(
                    "AUSBOSS_NODES_ImageResize", image=garment, target_mode="megapixels",
                    width=0, height=0, edge_length=1024, megapixels=1.0, scale_factor=0.5,
                    keep_proportion="fit", fill_color="#000000", divisible_by=32, interpolation="lanczos",
                )
                positive, negative, latent = comani.node(
                    "TextEncodeQwenImage21", clip=clip, vae=vae, prompt=prompt,
                    negative_prompt="", resolution=0, **{"images.image_1": person, "images.image_2": garment},
                )
                await asyncio.gather(positive, negative, latent)
                stages.append({"phase": "edit_encode", "seconds": time.perf_counter() - started})
                for index in range(args.count):
                    seed = args.seed + index
                    started = time.perf_counter()
                    samples = comani.node("KSampler", model=model, positive=positive, negative=negative,
                                          latent_image=latent, seed=seed, steps=args.steps, cfg=1.0,
                                          sampler_name="euler", scheduler="simple", denoise=1.0)
                    await samples
                    stages.append({"phase": "sample", "seed": seed, "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    dressed = comani.node("VAEDecode", samples=samples, vae=vae)
                    dressed, _ = comani.node("SplitImageWithAlpha", image=dressed)
                    await dressed
                    stages.append({"phase": "decode", "seed": seed, "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    saved = comani.node("SaveImage", images=dressed, filename_prefix="comani/outfit", return_call=True)
                    file = await saved.file()
                    stages.append({"phase": "save", "seconds": time.perf_counter() - started})
                    started = time.perf_counter()
                    target = args.output_dir / f"seed{seed}-outfit.png"
                    await comani.files.download(file, target)
                    stages.append({"phase": "download", "bytes": file.size, "seconds": time.perf_counter() - started})
                    print(target, flush=True)
            finally:
                await asyncio.gather(*(comani.files.remove(file) for file in uploads))
    except Exception as error:
        profile["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        profile["total_seconds"] = time.perf_counter() - total_started
        (args.output_dir / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--person", type=Path, required=True)
    parser.add_argument("--garment", type=Path, required=True)
    parser.add_argument("--description", help="manual alternative to vision-generated garment description")
    parser.add_argument("--who", default="the person")
    parser.add_argument("--instruction", default="Replace everything they wear.")
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--count", type=int, default=1, help="new products at seed, seed+1, ...; models stay loaded")
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/qwen-outfit"))
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--install-plugin", action="store_true",
                        help="install AusBoss if missing; only then stop the runtime")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    asyncio.run(main(args))
