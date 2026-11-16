"""Prepare one GPU instance and run H3 through eager, captured and native graphs.

COMANI_URL / COMANI_TOKEN select the management daemon. All --runtime-*,
--comfy-root and --*-root paths are on the server; --output is local.
--prepare explicitly installs selected versions; without it this script checks
the existing environment, verifies/reuses the three weights, and starts compute.
Default output is native SaveImage 8-bit PNG. --fetch-tensors additionally saves
precise float tensors and checks equality; it requires local Torch/NumPy/Pillow.
Historical GPU reports describe the older script hashes, not this rewrite.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from comani import Client

COMFY_COMMIT = "eee59185ff4f43ae2d9ad122b245619d27d9daad"
CLI_COMMIT = "4f554bb5a9e5d0b5d04146e7bdf8cac2ceb729a5"
HF_COMMIT = "0bb149f51e3156ed4b90dd45d5620346bfe1df8f"
MODEL = "minimax_H3_image_pruned_INT8.safetensors"
MODEL_SHA256 = "4647bdf553a32aa4ae55d9eb28322be42d32611d333c19a097d2845ae8a9b7a8"
ENCODER = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE = "minimax_h3_image_vae_fp16.safetensors"
PROMPT = (
    "A detailed studio photograph of a small ceramic red fox sitting on a dark blue "
    "wooden table. The fox has white ear tips and a white chest, delicate painted "
    "fur grooves, and a soft satin glaze. Warm sunlight enters from the upper left. "
    "A small green plant and a closed cream sketchbook sit behind it. The fox is "
    "centered, fully visible, with crisp detail, believable shadows, and no lettering."
)
SEED, WIDTH, HEIGHT, STEPS = 426190, 2048, 2048, 6
OPS = Path(__file__).with_name("minimax_h3_source_ops.py")
WEIGHTS = (
    ("diffusion_models", MODEL,
     {"model_id": 3045867, "version_id": 3462708, "file_id": 3365259},
     MODEL_SHA256),
    ("text_encoders", ENCODER,
     {"repo_id": "Comfy-Org/MiniMax-H3-Image", "revision": HF_COMMIT,
      "path": "text_encoders/" + ENCODER},
     "35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6"),
    ("vae", VAE,
     {"repo_id": "Comfy-Org/MiniMax-H3-Image", "revision": HF_COMMIT, "path": "vae/" + VAE},
     "d80e9c2e5cfc9844869961ff5a58c9a9e52182df6bfb30b26018dd7f348721af"),
)


def add_arguments(parser):
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--output", required=True, type=Path, help="local output directory")
    parser.add_argument("--prepare", action="store_true", help="explicitly modify the stopped target")
    parser.add_argument("--new-environment", help="new absolute server venv directory")
    parser.add_argument("--runtime-python", help="existing server target interpreter")
    parser.add_argument("--comfy-root", help="server ComfyUI source directory")
    parser.add_argument("--comfy-commit", default=COMFY_COMMIT, help="required complete core commit")
    parser.add_argument("--runtime-package", help="exact server wheel path or pinned requirement")
    parser.add_argument("--hub-version", help="optional exact huggingface-hub version to install")
    parser.add_argument("--torch-requirement", action="append", default=[],
                        help="explicit pinned GPU package requirement; repeat for companion packages")
    parser.add_argument("--torch-index-url", help="explicit GPU wheel index for those requirements")
    parser.add_argument("--expected-python", help="required target Python version, e.g. 3.12.15")
    parser.add_argument("--expected-torch", help="required installed Torch version, including build suffix")
    parser.add_argument("--plugin", action="append", default=[], help="explicit registry id@version")
    parser.add_argument("--git-plugin", action="append", default=[], help="repository-url@full-commit")
    parser.add_argument("--diffusion-root", help="absolute server diffusion_models root")
    parser.add_argument("--encoder-root", help="absolute server text_encoders root")
    parser.add_argument("--vae-root", help="absolute server vae root")
    parser.add_argument("--transport", choices=("auto", "http", "aria2"), default="auto")
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--fetch-tensors", action="store_true", help="also fetch and compare exact floats")
    parser.add_argument("--wheel-sha256", help="optional provenance label; not an installation verifier")


async def choose_root(remote, category, selected_path):
    roots = await remote.files.roots(category=category)
    selected = [root for root in roots if root.path == selected_path] if selected_path else list(roots)
    if len(selected) != 1:
        raise ValueError(f"Choose an explicit {category} root from {[root.path for root in roots]}")
    return selected[0]


async def prepare(remote, args):
    """Ordinary sequential Python: explicit preparation then a model-ready barrier."""
    info = await remote.comfyui.info()
    if args.prepare:
        if info.state == "running":
            raise ValueError("Stop the runtime explicitly before --prepare")
        if not args.runtime_package:
            raise ValueError("--prepare requires --runtime-package")
        if args.new_environment and args.runtime_python:
            raise ValueError("Select --new-environment or --runtime-python")
        if args.new_environment:
            if not args.torch_requirement or not args.torch_index_url:
                raise ValueError("New GPU environment needs pinned --torch-requirement and --torch-index-url")
            env = await remote.comfyui.create_environment(args.new_environment)
            runtime_python = env.python
        else:
            runtime_python = args.runtime_python or info.runtime_python
        fields = {"runtime_python": runtime_python, "cpu": False, "custom_nodes": True}
        if args.comfy_root:
            fields["comfy_root"] = args.comfy_root
        await remote.comfyui.configure(**fields)
        await remote.comfyui.prepare_tools(cli_revision=CLI_COMMIT, manager_version="4.2.2",
                                           hub_version=args.hub_version)
        if args.torch_requirement:
            if not args.torch_index_url:
                raise ValueError("GPU package installation needs --torch-index-url")
            command = [runtime_python, "-m", "pip", "install", *args.torch_requirement,
                       "--index-url", args.torch_index_url]
            await remote.bash.run(shlex.join(command), check=True)
        configured = await remote.comfyui.info()
        if configured.installed:
            await remote.comfyui.update(commit=args.comfy_commit, skip_torch=True)
        else:
            await remote.comfyui.install(commit=args.comfy_commit, skip_torch=True)
        await remote.comfyui.prepare_runtime(args.runtime_package)
        for spec in args.plugin:
            package, version = spec.rsplit("@", 1)
            await remote.plugins.install([package], version=version)
        for spec in args.git_plugin:
            repository, commit = spec.rsplit("@", 1)
            await remote.plugins.install_git(repository, commit=commit)
    elif any((args.new_environment, args.runtime_python, args.comfy_root, args.runtime_package,
              args.torch_requirement, args.plugin, args.git_plugin, args.hub_version)):
        raise ValueError("Environment changes require --prepare")

    info = await remote.comfyui.info()
    if not info.installed or info.config.get("cpu"):
        raise ValueError("H3 requires an installed GPU-configured runtime")
    source = info.extra.get("source", {})
    if source.get("commit") != args.comfy_commit:
        raise ValueError(f"Target core commit is {source.get('commit')}; expected {args.comfy_commit}")
    environment = info.extra.get("environment", {})
    if args.expected_python and environment.get("python_version") != args.expected_python:
        raise ValueError("Target Python version does not match --expected-python")
    torch_version = environment.get("packages", {}).get("torch", {}).get("version")
    if args.expected_torch and torch_version != args.expected_torch:
        raise ValueError(f"Target Torch is {torch_version}; expected {args.expected_torch}")

    requested_roots = (args.diffusion_root, args.encoder_root, args.vae_root)

    async def acquire(weight, selected_path):
        category, name, source, expected_hash = weight
        root = await choose_root(remote, category, selected_path)
        resolved = await remote.models.resolve(source)
        candidates = [artifact for artifact in resolved.files if artifact.filename == name]
        if len(candidates) != 1:
            raise ValueError(f"The explicit {name} selection resolved to {len(candidates)} candidates")
        artifact = candidates[0]
        if artifact.sha256 and artifact.sha256.lower() != expected_hash:
            raise ValueError(f"Provider digest changed for {name}")
        artifact = replace(artifact, sha256=expected_hash)
        return await remote.models.download(artifact, root=root, path=name, reuse=True,
                                            transport=args.transport, connections=args.connections)

    # Different categories/roots form a readiness barrier, not a cross-filesystem transaction.
    files = await asyncio.gather(*(acquire(weight, root)
                                   for weight, root in zip(WEIGHTS, requested_roots)))
    if info.state != "running":
        await remote.comfyui.start()
    return {"info": asdict(await remote.comfyui.info()), "models": [asdict(file) for file in files]}


def identity(remote, value, *, source=False):
    if source:
        return remote.python(source=OPS, entry="identity", inputs={"value": value})
    return remote.python(lambda value: value, inputs={"value": value})


def pipeline(remote, *, source=False, seed=SEED):
    clip = remote.node("CLIPLoader", clip_name=ENCODER, type="minimax", device="default")
    positive, latent = remote.node("MiniMaxH3ImageConditioning", clip=clip,
                                   prompt=PROMPT, width=WIDTH, height=HEIGHT)
    negative = remote.node("ConditioningZeroOut", conditioning=positive)
    model = remote.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
    sampled = remote.node("KSampler", model=model, positive=positive,
                          negative=negative, latent_image=latent,
                          seed=seed, steps=STEPS, cfg=1.0, sampler_name="euler",
                          scheduler="simple", denoise=1.0)
    passed = identity(remote, sampled, source=source)
    vae = remote.node("VAELoader", vae_name=VAE)
    return remote.node("VAEDecode", samples=passed.output(), vae=vae)


def native_prompt():
    return {
        "clip": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": ENCODER, "type": "minimax", "device": "default"}},
        "condition": {"class_type": "MiniMaxH3ImageConditioning", "inputs": {
            "clip": ["clip", 0], "prompt": PROMPT, "width": WIDTH, "height": HEIGHT}},
        "negative": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["condition", 0]}},
        "model": {"class_type": "UNETLoader", "inputs": {"unet_name": MODEL, "weight_dtype": "default"}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["model", 0], "positive": ["condition", 0], "negative": ["negative", 0],
            "latent_image": ["condition", 1], "seed": SEED, "steps": STEPS, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
    }


async def download_png(remote, image, local_path, prefix):
    saved = remote.node("SaveImage", images=image, filename_prefix=prefix, return_call=True)
    await saved
    metadata = await saved.metadata()
    item = metadata.ui["result"]["images"][0]
    path = str(PurePosixPath(item["subfolder"]) / item["filename"])
    file = await remote.files.stat(path, directory=item["type"])
    result = await remote.files.download(file, local_path)
    return {"png": str(result.local_path), "png_sha256": result.sha256,
            "remote_png": file.native_name, "save_call_id": saved.id}


def save_image(image, name, directory):
    """Explicit benchmark output: preserve float Tensor and derive a diagnostic PNG."""
    import numpy as np
    import torch
    from PIL import Image

    image = image.detach().cpu()
    assert tuple(image.shape) == (1, HEIGHT, WIDTH, 3) and torch.isfinite(image).all()
    png, floats = directory / (name + ".png"), directory / (name + ".pt")
    Image.fromarray(np.clip(image[0].numpy() * 255, 0, 255).astype(np.uint8)).save(png)
    torch.save(image, floats)
    return image, {"png": str(png.resolve()), "float_tensor": str(floats.resolve()),
                   "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                   "float_sha256": hashlib.sha256(floats.read_bytes()).hexdigest()}


async def run(args, *, source=True):
    args.output.mkdir(parents=True, exist_ok=True)
    record = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "source_mode": source, "seed": SEED, "prompt": PROMPT,
              "source_ops_sha256": hashlib.sha256(OPS.read_bytes()).hexdigest() if source else None,
              "width": WIDTH, "height": HEIGHT, "steps": STEPS,
              "model": MODEL, "model_sha256": MODEL_SHA256,
              "wheel_sha256_label": args.wheel_sha256, "runs": []}
    eager = None
    async with Client(args.url, token=os.environ.get("COMANI_TOKEN")) as remote:
        record["remote"] = await prepare(remote, args)
        for mode in ("eager", "captured_graph", "native_api"):
            started = time.monotonic()
            if mode == "eager":
                output = pipeline(remote, source=source)
            elif mode == "captured_graph":
                with remote.graph() as graph:
                    graph.output("image", pipeline(remote, source=source))
                output = remote.submit(graph).output("image")
            else:
                prompt = native_prompt()
                (args.output / "native_prompt.json").write_text(json.dumps(prompt, indent=2) + "\n")
                output = remote.submit(prompt, outputs={"image": ["decode", 0]},
                                       targets=["decode"]).output("image")
            row = {"mode": mode}
            if args.fetch_tensors:
                kept = identity(remote, output, source=source)
                ref = await kept.output()
                try:
                    tensor = await ref.fetch()
                    # This diagnostic path intentionally includes local Tensor dependencies.
                    image, files = save_image(tensor, mode + "-float-preview", args.output)
                    row.update(files)
                    if eager is None:
                        eager = image
                    else:
                        import torch

                        torch.testing.assert_close(eager, image, rtol=0, atol=0)
                        row["max_abs_float_error_vs_eager"] = float((eager - image).abs().max())
                    row.update(await download_png(remote, ref, args.output / (mode + ".png"),
                                                  "comani/h3/" + mode))
                finally:
                    ref.release()
            else:
                row.update(await download_png(remote, output, args.output / (mode + ".png"),
                                              "comani/h3/" + mode))
            row["seconds"] = time.monotonic() - started
            record["runs"].append(row)
            (args.output / "run.json").write_text(json.dumps(record, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    record["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    record["passed"] = True
    (args.output / "run.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument("--callable", action="store_true",
                        help="use callable identity; client/target Python and cloudpickle must match")
    args = parser.parse_args()
    asyncio.run(run(args, source=not args.callable))
