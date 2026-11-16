"""Save a scene, disconnect, continue its project in a new Client, then export.

Independent adaptation of LongForge v7.8 (resource2926029/version3467080).
The original MIT LongForge NodePack is published in the official archive
declared below. --install-plugin permits daemon-side acquisition and installation;
that official-source path still needs independent remote validation.
Use --download-models to prepare the weights before execution. This small REF2VA configuration
uses existing Singularity REF2VA weights, 544x960/2 new seconds, HARD22 overlap,
and original20-step Euler/simple sampling. The supplied GUI has model placeholders,
768x1344/10 new seconds, SOFT5 overlap and optional Sage/VEDA preview patches.
Assistant, RefMods, HQ, cache, VFX and interpolation are omitted explicitly.
No LoRA is added: the source Turbo and Extra LoRA loaders are bypassed.

python examples/h3_longforge.py --output-dir .local/longforge
Models and outputs retain the H3 license restrictions. Each invocation creates
a fresh local run-* directory and, by default, a unique remote project name.
The final export is downloaded; native immutable takes remain in the project.
"""

import argparse
import asyncio
import json
import os
import shlex
import tempfile
import uuid
from pathlib import Path

import comani

NODE_ARCHIVE = {
    "url": "https://civitai.com/api/download/models/3467080?fileId=3370455",
    "sha256": "5754f4594ed185577da67e1b4015139793bbfdbbeca0ef33b0a378b8185ccc0d",
    "size": 649345,
    "packages": {"ComfyUI-H3-LongForge-NodePack": "ComfyUI-H3-LongForge-NodePack"},
}

STORY = [
    {"name": "Lake", "prompt": "VIDEO: A copper toy sailboat drifts across a calm garden pond in warm morning light. "
     "The camera tracks gently beside it in one continuous shot. SOUND: Quiet ripples and distant birds. MUSIC: N/A"},
    {"name": "Reeds", "prompt": "VIDEO: The same copper toy sailboat continues beside tall green reeds at the edge "
     "of the pond. Preserve the boat, warm morning light and smooth camera motion. "
     "SOUND: The same quiet ripples and distant birds continue. MUSIC: N/A"},
]
WEIGHTS = (
    ("diffusion_models", "WarmBloodAban/Minimax-h3_Singularity",
     "Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors"),
    ("text_encoders", "Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    ("vae", "Kijai/MiniMax-H3-experimental", "minimax_h3_video_vae_int8_convrot.safetensors"),
    ("vae", "Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors"),
)


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    project = args.project or "comani-" + uuid.uuid4().hex[:12]
    story = json.loads(args.story.read_text()) if args.story else STORY
    if not isinstance(story, list) or len(story) != 2:
        raise ValueError("This continuation example needs exactly two scene cards")
    print(f"Output directory: {args.output_dir}\nRemote project: {project}", flush=True)

    # Each Client owns its objects; only the native project/takes survive reconnecting.
    for scene_number in (1, 2):
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            if scene_number == 1 and args.install_plugin:
                installed = await comani.plugins.list()
                missing = {name: path for name, path in NODE_ARCHIVE["packages"].items() if not any(
                    Path(plugin.path).name.casefold() == name.casefold() for plugin in installed)}
                info = await comani.comfyui.info()
                dependencies = await comani.bash.run(shlex.join([info.runtime_python, "-c",
                    "import importlib.metadata,importlib.util,shutil; "
                    "assert int(importlib.metadata.version('filelock').split('.')[0]) >= 3; "
                    "assert shutil.which('ffmpeg') or importlib.util.find_spec('imageio_ffmpeg')"]), check=False)
                if missing or dependencies.exit_code:
                    await comani.comfyui.stop()
                if missing:
                    await comani.plugins.install_archive({**NODE_ARCHIVE, "packages": missing})
                if dependencies.exit_code:
                    await comani.bash.run(shlex.join([
                        info.runtime_python, "-m", "pip", "install", "filelock>=3", "imageio-ffmpeg"]))
            info = await comani.comfyui.start()
            required = {"LongForgeProject", "LongForgeVideoSettings", "LongForgeSampling", "LongForgeRender"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Install original LongForge NodePack first; missing: {sorted(missing)}")
            if scene_number == 1 and args.download_models:
                sources = await asyncio.gather(*(comani.models.resolve({"repo_id": repo, "path": path})
                                                 for _, repo, path in WEIGHTS))
                downloads = await asyncio.gather(*(comani.models.download(
                    source.files[0], category=category, path=Path(path).name, reuse=True)
                    for (category, _, path), source in zip(WEIGHTS, sources)), return_exceptions=True)
                for result in downloads:
                    if isinstance(result, BaseException):
                        raise result

            model = comani.node("UNETLoader", unet_name=args.model, weight_dtype="default")
            clip = comani.node("CLIPLoader", clip_name=args.clip, type="minimax", device="default")
            video_vae = comani.node("VAELoader", vae_name=args.video_vae)
            audio_vae = comani.node("VAELoader", vae_name=args.audio_vae)
            await asyncio.gather(model, clip, video_vae, audio_vae)
            video = comani.node(
                "LongForgeVideoSettings", pipeline="REF2VA", width=args.width, height=args.height,
                frames=107, overlap_frames=args.overlap, continuity_mode="MANUAL",
                audio_continuity="HARD", audio_history_frames=0, reference_policy="EVERY SCENE",
                ref_image_size="max", last_frame_usage="LAST SCENE", audio_soft_steps=4,
                duration_mode="NEW SECONDS", new_seconds=args.new_seconds, continuation_method="PROTECTED")
            sampling = comani.node("LongForgeSampling", steps=args.steps, sampler="euler", scheduler="simple",
                                  denoise=1.0, cfg=1.0, seed=args.seed, increment_seed=True,
                                  shift_video=12.0, shift_audio=3.0)
            actions = ("GENERATE NEXT",) if scene_number == 1 else ("GENERATE NEXT", "EXPORT FILM")
            for action in actions:
                exporting = action == "EXPORT FILM"
                job = comani.node("LongForgeProject", project_name=project, action=action,
                                  run_mode="ONE SCENE", scene_data=json.dumps(story), selected_scene=1,
                                  run_stamp=3 if exporting else scene_number, export_when_done=False)
                rendered = comani.node(
                    "LongForgeRender", job=job, video_settings=video, sampling=sampling,
                    model=model, clip=clip, video_vae=video_vae, audio_vae=audio_vae,
                    preview_new_scene=True, export_crf=18, repair_audio_seams=True, repair_video_seams=False,
                    fade_film_edges=True, encoding_speed="medium", metadata_mode="NONE",
                    diagnostic_wavs=False, return_call=True)
                report = json.loads(await (await rendered.output(1)).fetch())
                name = "export" if exporting else f"scene-{scene_number}"
                (args.output_dir / f"{name}.json").write_text(json.dumps(report, indent=2))
                print(f"{name}: {report.get('summary', 'completed')}", flush=True)
                if exporting:
                    target = args.output_dir / "film.mp4"
                    await comani.files.download(await rendered.file(), target)
                    print(target, flush=True)
        print(f"Client {scene_number} closed; project takes remain on the daemon disk.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--project", help="existing project name to continue; default creates a new unique project")
    parser.add_argument("--story", type=Path, help="JSON list of exactly two scene cards with name/prompt")
    parser.add_argument("--install-plugin", action="store_true",
                        help="install the missing original NodePack from its official archive on the daemon")
    parser.add_argument("--download-models", action="store_true",
                        help="acquire the four default H3 weights through the daemon")
    parser.add_argument("--model", default="Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors")
    parser.add_argument("--clip", default="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    parser.add_argument("--video-vae", default="minimax_h3_video_vae_int8_convrot.safetensors")
    parser.add_argument("--audio-vae", default="minimax_h3_audio_vae_fp32.safetensors")
    parser.add_argument("--width", type=int, default=544)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--new-seconds", type=float, default=2.0)
    parser.add_argument("--overlap", type=int, default=22)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=201)
    parser.add_argument("--output-dir", type=Path, default=Path(".local/longforge"))
    args = parser.parse_args()
    if args.download_models and (args.model, args.clip, args.video_vae, args.audio_vae) != tuple(
        Path(weight[2]).name for weight in WEIGHTS
    ):
        parser.error("--download-models prepares the default weight set; preinstall custom selections")
    asyncio.run(main(args))
