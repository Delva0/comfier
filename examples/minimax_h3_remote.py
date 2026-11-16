"""Reuse or explicitly prepare a GPU instance, acquire H3 weights and save one PNG.

COMANI_URL / COMANI_TOKEN select the daemon. Environment/model-root paths are
on the server; --output is local. Ordinary runs reuse installed software.
--prepare permits environment/plugin changes; --update or an explicit core
version/commit also selects an update. Torch/CUDA packages are your choice.
"""

import argparse
import asyncio
import os
import shlex
from dataclasses import replace
from pathlib import Path

import comani

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


def plugin_spec(value):
    package, separator, version = value.rpartition("@")
    if not separator or not package or not version:
        raise argparse.ArgumentTypeError("use registry-id@version")
    return package, version


def git_plugin_spec(value):
    repository, separator, commit = value.rpartition("@")
    if not separator or not repository or len(commit) != 40 or any(c not in "0123456789abcdefABCDEF" for c in commit):
        raise argparse.ArgumentTypeError("use repository-url@complete-40-character-commit")
    return repository, commit


def add_arguments(parser, *, benchmark=False):
    parser.add_argument("--url", type=str,
                        default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/h3-benchmark" if benchmark else "artifacts/h3.png"),
                        help="local benchmark directory" if benchmark else "new local PNG")
    parser.add_argument("--prompt", type=str, default=PROMPT)
    if not benchmark:
        parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument("--steps", type=int, default=STEPS)
    preparation = parser.add_argument_group("explicit environment preparation (requires --prepare)")
    preparation.add_argument("--prepare", action="store_true", help="modify the stopped target; reuse installed core/tools")
    preparation.add_argument("--new-environment", type=str, help="new absolute server venv directory")
    preparation.add_argument("--runtime-python", type=str, help="existing server target interpreter")
    preparation.add_argument("--comfy-root", type=str, help="server ComfyUI source directory")
    preparation.add_argument("--runtime-package", type=str, help="server wheel path or chosen requirement")
    preparation.add_argument("--update", action="store_true", help="update existing core along its current upstream")
    core = preparation.add_mutually_exclusive_group()
    core.add_argument("--comfy-version", type=str, help="explicit core channel/tag, e.g. nightly or latest")
    core.add_argument("--comfy-commit", type=str, help="explicit complete core commit; also permits updating")
    cli = preparation.add_mutually_exclusive_group()
    cli.add_argument("--cli-version", type=str, help="explicit comfy-cli PyPI version")
    cli.add_argument("--cli-revision", type=str, help="explicit comfy-cli Git ref")
    preparation.add_argument("--manager-version", type=str, help="explicit packaged Manager version")
    preparation.add_argument("--hub-version", type=str, help="explicit huggingface-hub version to install")
    preparation.add_argument("--torch-requirement", type=str, action="append", default=[],
                             help="chosen GPU package requirement; repeat for companion packages")
    preparation.add_argument("--torch-index-url", type=str, help="GPU wheel index for those requirements")
    preparation.add_argument("--plugin", type=plugin_spec, action="append", default=[],
                             metavar="ID@VERSION", help="install a selected registry plugin")
    preparation.add_argument("--git-plugin", type=git_plugin_spec, action="append", default=[],
                             metavar="URL@COMMIT", help="install a selected Git plugin")
    storage = parser.add_argument_group("remote model storage and transfer")
    storage.add_argument("--diffusion-root", type=str, help="absolute server diffusion_models root")
    storage.add_argument("--encoder-root", type=str, help="absolute server text_encoders root")
    storage.add_argument("--vae-root", type=str, help="absolute server vae root")
    storage.add_argument("--transport", choices=("auto", "http", "aria2"), default="auto")
    storage.add_argument("--connections", type=int, default=8)


async def choose_root(category, selected_path):
    roots = await comani.files.roots(category=category)
    selected = [root for root in roots if root.path == selected_path] if selected_path else roots
    if len(selected) != 1:
        raise ValueError(f"Choose an explicit {category} root from {[root.path for root in roots]}")
    return selected[0]


async def prepare(args):
    """Explicit changes first; three verified model downloads form the ready barrier."""
    info = await comani.comfyui.info()
    changes = (args.new_environment, args.runtime_python, args.comfy_root, args.runtime_package,
               args.update, args.comfy_version, args.comfy_commit, args.cli_version, args.cli_revision,
               args.manager_version, args.hub_version, args.torch_requirement, args.torch_index_url,
               args.plugin, args.git_plugin)
    if not args.prepare and any(changes):
        raise ValueError("Environment changes require --prepare")
    if args.prepare:
        if info.state == "running":
            raise ValueError("Stop the runtime explicitly before --prepare")
        if args.new_environment and args.runtime_python:
            raise ValueError("Select --new-environment or --runtime-python")
        runtime_python = args.runtime_python or info.runtime_python
        if args.new_environment:
            if not (args.runtime_package and args.torch_requirement and args.torch_index_url):
                raise ValueError("New GPU environment needs --runtime-package, --torch-requirement and --torch-index-url")
            runtime_python = (await comani.comfyui.create_environment(args.new_environment)).python
        fields = {"runtime_python": runtime_python, "cpu": False, "custom_nodes": True}
        if args.comfy_root:
            fields["comfy_root"] = args.comfy_root
        await comani.comfyui.configure(**fields)
        await comani.comfyui.prepare_tools(cli_version=args.cli_version, cli_revision=args.cli_revision,
                                            skip_manager=True)
        if args.torch_requirement:
            if not args.torch_index_url:
                raise ValueError("GPU package installation needs --torch-index-url")
            command = [runtime_python, "-m", "pip", "install", *args.torch_requirement,
                       "--index-url", args.torch_index_url]
            await comani.bash.run(shlex.join(command))
        configured = await comani.comfyui.info()
        target = {"version": args.comfy_version, "commit": args.comfy_commit,
                  "skip_torch": True, "skip_manager": True}
        if not configured.installed:
            await comani.comfyui.install(**target)
        elif args.update or args.comfy_version or args.comfy_commit:
            await comani.comfyui.update(**target)
        if args.runtime_package:
            await comani.comfyui.prepare_runtime(args.runtime_package)
        await comani.comfyui.prepare_tools(skip_cli=True, manager_version=args.manager_version,
                                            hub_version=args.hub_version)
        for package, version in args.plugin:
            await comani.plugins.install([package], version=version)
        for repository, commit in args.git_plugin:
            await comani.plugins.install_git(repository, commit=commit)

    info = await comani.comfyui.info()
    if not info.installed or info.config.get("cpu"):
        raise ValueError("H3 needs an installed GPU-configured runtime; select --prepare to change it")
    # Check actual node availability, rather than requiring a particular software commit.
    info = await comani.comfyui.start()
    nodes = (info.extra.get("node_metadata") or {}).get("nodes", {})
    required = {"CLIPLoader", "MiniMaxH3ImageConditioning", "ConditioningZeroOut",
                "UNETLoader", "KSampler", "VAELoader", "VAEDecode", "SaveImage"}
    missing = required - nodes.keys()
    if missing:
        raise ValueError(f"The selected core/plugins lack H3 nodes: {sorted(missing)}")

    async def acquire(weight, selected_path):
        category, name, source, expected_hash = weight
        root = await choose_root(category, selected_path)
        resolved = await comani.models.resolve(source)  # Local provider query; no remote disk access.
        candidates = [artifact for artifact in resolved.files if artifact.filename == name]
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one provider file named {name}")
        artifact = candidates[0]
        if artifact.sha256 and artifact.sha256.lower() != expected_hash:
            raise ValueError(f"Provider digest changed for {name}")
        artifact = replace(artifact, sha256=expected_hash)
        return await comani.models.download(artifact, root=root, path=name, reuse=True,
                                             transport=args.transport, connections=args.connections)

    # Different roots/providers are not one filesystem transaction.
    roots = (args.diffusion_root, args.encoder_root, args.vae_root)
    acquired = await asyncio.gather(*(acquire(weight, root) for weight, root in zip(WEIGHTS, roots)))
    return acquired


async def run(args, *, transform=None):
    async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
        await prepare(args)
        with comani.graph() as graph:
            clip = comani.node("CLIPLoader", clip_name=ENCODER, type="minimax", device="default")
            positive, latent = comani.node("MiniMaxH3ImageConditioning", clip=clip,
                                           prompt=args.prompt, width=args.width, height=args.height)
            negative = comani.node("ConditioningZeroOut", conditioning=positive)
            model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
            sampled = comani.node(
                "KSampler", model=model, positive=positive, negative=negative, latent_image=latent,
                seed=args.seed, steps=args.steps, cfg=1.0, sampler_name="euler",
                scheduler="simple", denoise=1.0,
            )
            vae = comani.node("VAELoader", vae_name=VAE)
            image = comani.node("VAEDecode", samples=sampled, vae=vae)
            if transform is not None:
                image = transform(image)
            saved = comani.node("SaveImage", images=image, filename_prefix="comani/h3", return_call=True)
        run = comani.submit(graph, return_call=True)
        output = await run.file(node_id=saved.id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        print((await comani.files.download(output, args.output)).local_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    asyncio.run(run(parser.parse_args()))
