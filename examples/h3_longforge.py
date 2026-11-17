"""Continue a native project, or replace a saved scene, review trim and export.

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

Inspect an existing project without loading models:
python examples/h3_longforge.py --project my-film --inspect
Then supply --edit edit.json with scene, prompt, revision, trim_revision and an
explicit trim policy: "restore", "full", or {"in_frame": 3, "out_frame": 80}.
REPLACE SELECTED is experimental. It retains successor latent payloads and pins
the old tail; visual/audio joins still need author review after export.
"""

import argparse
import asyncio
import json
import os
import shlex
import tempfile
import uuid
from pathlib import Path

import comfier as cf

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


async def prepare_runtime(args, *, prepare=False, download=True):
    if prepare and args.install_plugin:
        installed = await cf.plugins.list()
        missing = {name: path for name, path in NODE_ARCHIVE["packages"].items() if not any(
            Path(plugin.path).name.casefold() == name.casefold() for plugin in installed)}
        info = await cf.comfyui.info()
        dependencies = await cf.bash.run(shlex.join([info.runtime_python, "-c",
            "import importlib.metadata,importlib.util,shutil; "
            "assert int(importlib.metadata.version('filelock').split('.')[0]) >= 3; "
            "assert shutil.which('ffmpeg') or importlib.util.find_spec('imageio_ffmpeg')"]), check=False)
        if missing or dependencies.exit_code:
            await cf.comfyui.stop()
        if missing:
            await cf.plugins.install_archive({**NODE_ARCHIVE, "packages": missing})
        if dependencies.exit_code:
            await cf.bash.run(shlex.join([
                info.runtime_python, "-m", "pip", "install", "filelock>=3", "imageio-ffmpeg"]))
    info = await cf.comfyui.start()
    required = {"LongForgeProject", "LongForgeVideoSettings", "LongForgeSampling", "LongForgeRender"}
    if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
        raise ValueError(f"Install original LongForge NodePack first; missing: {sorted(missing)}")
    if prepare and download and args.download_models:
        sources = await asyncio.gather(*(cf.models.resolve({"repo_id": repo, "path": path})
                                         for _, repo, path in WEIGHTS))
        downloads = await asyncio.gather(*(cf.models.download(
            source.files[0], category=category, path=Path(path).name, reuse=True)
            for (category, _, path), source in zip(WEIGHTS, sources)), return_exceptions=True)
        for result in downloads:
            if isinstance(result, BaseException):
                raise result


async def edit_project(args, project):
    edit = json.loads(args.edit.read_text()) if args.edit else None
    if edit is not None:
        if (not isinstance(edit, dict) or type(edit.get("scene")) is not int
                or edit["scene"] < 1 or not isinstance(edit.get("prompt"), str)
                or not edit["prompt"].strip() or not isinstance(edit.get("revision"), str)
                or "trim_revision" not in edit or "trim" not in edit):
            raise ValueError("Edit needs scene, prompt, revision, trim_revision and explicit trim policy")
        trim = edit["trim"]
        if trim not in ("restore", "full") and (
                not isinstance(trim, dict) or set(trim) != {"in_frame", "out_frame"}
                or any(type(trim[key]) is not int for key in trim)
                or not 0 <= trim["in_frame"] < trim["out_frame"]):
            raise ValueError("trim must be restore, full or integer in_frame/out_frame range")

    def save(name, value):
        (args.output_dir / f"{name}.json").write_text(json.dumps(value, indent=2), encoding="utf-8")

    async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
        # Inspection and edit preflight need no weights. Acquisition follows
        # successful revision/trim checks, before the one replacement render.
        await prepare_runtime(args, prepare=True, download=False)
        status = await cf.comfyui.request("GET", "/longforge_unified/project", params={"name": project})
        save("before-project", status)
        if status.get("broken") or not status.get("head"):
            raise ValueError("Choose an intact saved project before inspecting or editing")
        revision = status["head"]["revision"]
        if args.inspect:
            for scene in range(1, status["head"]["cursor"] + 1):
                value = await cf.comfyui.request("GET", "/longforge_unified/scene", params={
                    "name": project, "index": scene, "revision": revision})
                save(f"scene-{scene}", value)
            print(f"Inspect before-project.json and scene-*.json in {args.output_dir}", flush=True)
            return
        if edit["revision"] != revision:
            raise ValueError("Project revision changed; inspect again before replacing a scene")
        selected = edit["scene"]
        before = await cf.comfyui.request("GET", "/longforge_unified/scene", params={
            "name": project, "index": selected, "revision": revision})
        save("before-scene", before)
        if before.get("missing") or before.get("damaged") or not before.get("export_trim"):
            raise ValueError("Selected scene or its export trim is unavailable")
        if edit["trim_revision"] != before["export_trim"]["revision"]:
            raise ValueError("Export trim changed; inspect again before approving its replacement range")
        trim = edit["trim"]
        if trim == "restore":
            trim = {key: before["export_trim"][key] for key in ("in_frame", "out_frame")}
        elif trim == "full":
            trim = {"in_frame": 0, "out_frame": before["visible_frames"]}
        if trim["out_frame"] > before["visible_frames"]:
            raise ValueError("Approved trim exceeds this scene's visible frames")
        story = status["story"]
        story[selected - 1] = {**story[selected - 1], "prompt": edit["prompt"]}
        plan = {"project": project, "revision": revision, "selected_scene": selected,
                "source_take": before["take"], "replace": [selected],
                "preserve_successor_payloads": list(range(selected + 1, status["head"]["cursor"] + 1)),
                "trim_after_replacement": trim, "trim_policy": edit["trim"],
                "note": "Replacement resets its trim. Apply this explicit approved range before exporting; review joins."}
        save("edit-plan", plan)
        print(json.dumps(plan, indent=2), flush=True)
        # Restore native settings so replacement retains temporal/spatial shape.
        settings = status["settings"]
        if (not settings or settings.get("hq", {}).get("mode", "OFF") != "OFF"
                or settings.get("refmods", {}).get("enabled", False)):
            raise ValueError("This small example requires saved base settings, HQ OFF and RefMods disabled")
        await prepare_runtime(args, prepare=True)
        video = cf.node("LongForgeVideoSettings", **settings["video"])
        sampling = cf.node("LongForgeSampling", **settings["sampling"])
        model = cf.node("UNETLoader", unet_name=args.model, weight_dtype="default")
        clip = cf.node("CLIPLoader", clip_name=args.clip, type="minimax", device="default")
        video_vae = cf.node("VAELoader", vae_name=args.video_vae)
        audio_vae = cf.node("VAELoader", vae_name=args.audio_vae)
        for stamp, action in enumerate(("REPLACE SELECTED", "EXPORT FILM"), 1):
            job = cf.node("LongForgeProject", project_name=project, action=action,
                              run_mode="ONE SCENE", scene_data=json.dumps(story), selected_scene=selected,
                              run_stamp=stamp, export_when_done=False)
            if action == "REPLACE SELECTED" and (await (await job).fetch())["head"]["revision"] != revision:
                raise ValueError("Project changed during edit preparation; no replacement submitted")
            rendered = cf.node(
                "LongForgeRender", job=job, video_settings=video, sampling=sampling,
                model=model, clip=clip, video_vae=video_vae, audio_vae=audio_vae,
                preview_new_scene=True, export_crf=18, repair_audio_seams=True, repair_video_seams=False,
                fade_film_edges=True, encoding_speed="medium", metadata_mode="NONE",
                diagnostic_wavs=False, return_call=True)
            report = json.loads(await (await rendered.output(1)).fetch())
            save("replace" if stamp == 1 else "export", report)
            if stamp == 1:
                after = await cf.comfyui.request("GET", "/longforge_unified/project", params={"name": project})
                current = await cf.comfyui.request("GET", "/longforge_unified/scene", params={
                    "name": project, "index": selected, "revision": after["head"]["revision"]})
                save("after-replace-scene", current)
                result = await cf.comfyui.request("POST", "/longforge_unified/export_trim", json={
                    "name": project, "revision": current["revision"], "scene": selected,
                    "take": current["take"], "trim_revision": current["export_trim"]["revision"], **trim})
                save("approved-trim", result)
            else:
                target = args.output_dir / "film.mp4"
                await cf.files.download(await rendered.file(), target)
                print(f"{target}\nReview the replaced performance and its video/audio joins.", flush=True)


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    project = args.project or "comfier-" + uuid.uuid4().hex[:12]
    print(f"Output directory: {args.output_dir}\nRemote project: {project}", flush=True)
    if args.edit or args.inspect:
        await edit_project(args, project)
        return
    story = json.loads(args.story.read_text()) if args.story else STORY
    if not isinstance(story, list) or len(story) != 2:
        raise ValueError("This continuation example needs exactly two scene cards")

    # Each Client owns its objects; only the native project/takes survive reconnecting.
    for scene_number in (1, 2):
        async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
            await prepare_runtime(args, prepare=scene_number == 1)

            model = cf.node("UNETLoader", unet_name=args.model, weight_dtype="default")
            clip = cf.node("CLIPLoader", clip_name=args.clip, type="minimax", device="default")
            video_vae = cf.node("VAELoader", vae_name=args.video_vae)
            audio_vae = cf.node("VAELoader", vae_name=args.audio_vae)
            await asyncio.gather(model, clip, video_vae, audio_vae)
            video = cf.node(
                "LongForgeVideoSettings", pipeline="REF2VA", width=args.width, height=args.height,
                frames=107, overlap_frames=args.overlap, continuity_mode="MANUAL",
                audio_continuity="HARD", audio_history_frames=0, reference_policy="EVERY SCENE",
                ref_image_size="max", last_frame_usage="LAST SCENE", audio_soft_steps=4,
                duration_mode="NEW SECONDS", new_seconds=args.new_seconds, continuation_method="PROTECTED")
            sampling = cf.node("LongForgeSampling", steps=args.steps, sampler="euler", scheduler="simple",
                                  denoise=1.0, cfg=1.0, seed=args.seed, increment_seed=True,
                                  shift_video=12.0, shift_audio=3.0)
            actions = ("GENERATE NEXT",) if scene_number == 1 else ("GENERATE NEXT", "EXPORT FILM")
            for action in actions:
                exporting = action == "EXPORT FILM"
                job = cf.node("LongForgeProject", project_name=project, action=action,
                                  run_mode="ONE SCENE", scene_data=json.dumps(story), selected_scene=1,
                                  run_stamp=3 if exporting else scene_number, export_when_done=False)
                rendered = cf.node(
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
                    await cf.files.download(await rendered.file(), target)
                    print(target, flush=True)
        print(f"Client {scene_number} closed; project takes remain on the daemon disk.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--project", help="existing project name to continue; default creates a new unique project")
    parser.add_argument("--story", type=Path, help="JSON list of exactly two scene cards with name/prompt")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true", help="save native project/scene snapshots without loading models")
    mode.add_argument("--edit", type=Path, help="approved JSON scene replacement and explicit export-trim policy")
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
    if (args.edit or args.inspect) and (not args.project or args.story):
        parser.error("--inspect/--edit needs --project and uses its saved story; omit --story")
    if args.download_models and (args.model, args.clip, args.video_vae, args.audio_vae) != tuple(
        Path(weight[2]).name for weight in WEIGHTS
    ):
        parser.error("--download-models prepares the default weight set; preinstall custom selections")
    asyncio.run(main(args))
