"""Generate floral studies with the source Ideogram4 sampler patches intact.

Independent eager adaptation of resource2674413/version3447447 (JSON v4.5).
The source Custom-size AGSoft node creates the same 128-channel zero latent
as core EmptyFlux2LatentImage: 1504x2200 becomes 1504x2192 after factor16 floor.
KJNodes and DetailDaemon supply the two remaining custom sampling nodes.
RES4LYF's beta57 registry alias is expressed by core BetaSamplingScheduler
with its exact original alpha0.5/beta0.7 parameters.
Weights use the Ideogram Non-Commercial Model Agreement; review their terms.

python examples/ideogram4.py --install-plugins --download-models --count 2
Each invocation creates a fresh run-* directory under the output root.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import comani

MODEL = "ideogram4_fp8_scaled.safetensors"
CLIP = "qwen3vl_8b_fp8_scaled.safetensors"
VAE = "flux2-vae.safetensors"
WEIGHTS = (("diffusion_models", MODEL), ("text_encoders", CLIP), ("vae", VAE))
PLUGINS = (
    ("comfyui-kjnodes", "https://github.com/kijai/ComfyUI-KJNodes"),
    ("comfyui-detail-daemon", "https://github.com/Jonseed/ComfyUI-Detail-Daemon"),
)
PROMPT = (
    "soft jewel tone, photorealistic macro floral texture, full frame, overhead flat lay, "
    "tightly layered dense blooms, one dominant large soft coral pink double hollyhock "
    "as focal point, surrounded by muted amethyst scabiosa, pale pearl orlaya, "
    "fine green fountain grass, fresh green foliage, soft diffused natural light, "
    "realistic macro floral photography,"
)


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass
    print(f"Output directory: {args.output_dir}", flush=True)
    profile = {"workflow_resource": 2674413, "workflow_version": 3447447,
               "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "effective_size": [args.width // 16 * 16, args.height // 16 * 16], "phases": []}
    stages = profile["phases"]
    total_started = time.perf_counter()
    try:
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            started = time.perf_counter()
            if args.install_plugins:
                installed = await comani.plugins.list()
                missing_plugins = [repository for name, repository in PLUGINS if not any(
                    plugin.id.casefold() == name or Path(plugin.path).name.casefold() == name
                    or (plugin.repository or "").rstrip("/").removesuffix(".git").casefold()
                    == repository.casefold() for plugin in installed
                )]
                if missing_plugins:
                    await comani.comfyui.stop()
                    for repository in missing_plugins:
                        await comani.plugins.install_git(repository)
            info = await comani.comfyui.start()
            required = {"Epsilon Scaling", "ModelSamplingAuraFlow", "CFGOverride", "ScheduledCFGGuidance",
                        "ExtendIntermediateSigmas", "LyingSigmaSampler", "EmptyFlux2LatentImage",
                        "BetaSamplingScheduler"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Missing backend nodes: {sorted(missing)}")
            stages.append({"phase": "connect_start", "seconds": time.perf_counter() - started})
            if args.download_models:
                for category, filename in WEIGHTS:
                    started = time.perf_counter()
                    source = await comani.models.resolve({"repo_id": "Comfy-Org/Ideogram-4",
                                                          "path": f"{category}/{filename}"})
                    artifact, = source.files
                    roots = await comani.files.roots(category=category)
                    if not roots:
                        raise ValueError(f"No native model roots for {category}")
                    existing = await comani.models.list(category=category, prefix=filename)
                    present = {f["root"]["id"] for f in existing["files"] if f["relative_path"] == filename}
                    root = min([r for r in roots if r.id in present] or roots, key=lambda r: r.priority)
                    await comani.models.download(artifact, root=root, path=filename, reuse=True)
                    stages.append({"phase": "model_download", "file": filename, "sha256": artifact.sha256,
                                   "revision": source.revision, "root": root.path,
                                   "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
            model = comani.node("Epsilon Scaling", model=model, scaling_factor=1.024)
            schedule_model = comani.node("ModelSamplingAuraFlow", model=model, shift=3.0, sampling="flow")
            guided_model = comani.node("CFGOverride", model=schedule_model, cfg=1.0,
                                      start_percent=0.9, end_percent=1.0)
            clip = comani.node("CLIPLoader", clip_name=CLIP, type="ideogram4", device="default")
            vae = comani.node("VAELoader", vae_name=VAE)
            positive = comani.node("CLIPTextEncode", clip=clip, text=args.prompt)
            negative = comani.node("CLIPTextEncode", clip=clip, text=args.negative)
            guider = comani.node("ScheduledCFGGuidance", model=guided_model, positive=positive, negative=negative,
                                cfg=4.0, start_percent=0.0, end_percent=1.0)
            sigmas = comani.node("BetaSamplingScheduler", model=schedule_model, steps=args.steps,
                                alpha=0.5, beta=0.7)
            sigmas = comani.node("ExtendIntermediateSigmas", sigmas=sigmas, steps=2,
                                start_at_sigma=1.0, end_at_sigma=0.98, spacing="linear")
            sampler = comani.node("KSamplerSelect", sampler_name="euler")
            sampler = comani.node("LyingSigmaSampler", sampler=sampler, dishonesty_factor=-0.02,
                                 start_percent=0.28, end_percent=0.95)
            latent = comani.node("EmptyFlux2LatentImage", width=args.width // 16 * 16,
                                height=args.height // 16 * 16, batch_size=1)
            await asyncio.gather(guider, sigmas, sampler, latent, vae)
            stages.append({"phase": "load_encode_schedule", "seconds": time.perf_counter() - started})
            for index in range(args.count):
                seed = args.seed + index
                started = time.perf_counter()
                noise = comani.node("RandomNoise", noise_seed=seed)
                samples, _denoised = comani.node("SamplerCustomAdvanced", noise=noise, guider=guider,
                                                sampler=sampler, sigmas=sigmas, latent_image=latent)
                image = comani.node("VAEDecode", samples=samples, vae=vae)
                await image
                stages.append({"phase": "sample_decode", "seed": seed, "seconds": time.perf_counter() - started})
                saved = comani.node("SaveImage", images=image, filename_prefix="comani/ideogram4", return_call=True)
                file = await saved.file()
                started = time.perf_counter()
                target = args.output_dir / f"seed{seed}.png"
                await comani.files.download(file, target)
                stages.append({"phase": "download", "seed": seed, "bytes": file.size,
                               "seconds": time.perf_counter() - started})
                print(target, flush=True)
    except Exception as error:
        profile["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        profile["total_seconds"] = time.perf_counter() - total_started
        (args.output_dir / "profile.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--negative", default="watermark")
    parser.add_argument("--width", type=int, default=1504)
    parser.add_argument("--height", type=int, default=2200)
    parser.add_argument("--steps", type=int, default=14)
    parser.add_argument("--seed", type=int, default=765752641118000)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--install-plugins", action="store_true")
    parser.add_argument("--download-models", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(".local/ideogram4"))
    args = parser.parse_args()
    if args.count < 1 or min(args.width, args.height) < 16:
        parser.error("--count must be positive and dimensions must be at least16")
    asyncio.run(main(args))
