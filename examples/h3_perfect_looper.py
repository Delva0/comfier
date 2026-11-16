"""Generate an anchor and a real two-ended masked AV bridge to close a loop.

Independent eager adaptation of CoolBreeze's PerfectLooper v1.1
(resource3023857/version3440094). Preserves original Singularity, Turbo-v4 at1,
SLA10%, shifts10/3, six Euler/beta steps, 56-frame anchor, trimmed8-frame ends,
39-frame protected AV windows and both linear crossfades/audio envelopes.
T2AV mode is used; optional image/video inputs, RTX and extra stitch frame are off.
GUI defaults0.7MP/4:3/7seconds yield992x736 and154 final frames (6.4167seconds).
The second saved video repeats the completed real loop only to inspect its seam.
Requires CoolB Nodes, H3-Motion-Context-MultiRef and KJNodes. Native core video
encoding replaces the GUI-only VHS previews. H3 model/output terms still apply.

python examples/h3_perfect_looper.py --install-plugins --download-models \
    --output-dir .local/perfect-looper
After preparation, omit those flags to reuse the installed packages and weights.
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import comani

LORA = "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors"
PLUGINS = (
    "https://github.com/CoolBreeze164/ComfyUI-CoolB-Nodes",
    "https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef",
    "https://github.com/kijai/ComfyUI-KJNodes",
)
WEIGHTS = (
    ("diffusion_models", "WarmBloodAban/Minimax-h3_Singularity",
     "Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors"),
    ("text_encoders", "Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    ("vae", "Kijai/MiniMax-H3-experimental", "minimax_h3_video_vae_int8_convrot.safetensors"),
    ("vae", "Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors"),
    ("loras", "drbaph/MiniMax-H3-Turbo-Lora-ComfyUI", LORA),
)
PROMPT = (
    "integrated_multimodal_description: [Shot 1] No video cuts. Looping video. Movement loops in the end.\n"
    "A cinematic side view of a small vintage plane flying through a dense mountain range. "
    "Tall thin stone pillars with jagged edges and sparse trees. The plane maneuvers smoothly "
    "between them while the camera follows it in one continuous shot.\n"
    "non_diegetic_music: Wind blowing, plane engine sounds."
)


async def acquire_model(category, repository, path, phases):
    started = time.perf_counter()
    source = await comani.models.resolve({"repo_id": repository, "path": path})
    artifact, = source.files
    filename = Path(path).name
    roots = await comani.files.roots(category=category)
    if not roots:
        raise ValueError(f"No native model roots for {category}")
    existing = await comani.models.list(category=category, prefix=filename)
    present = {f["root"]["id"] for f in existing["files"] if f["relative_path"] == filename}
    root = min([r for r in roots if r.id in present] or roots, key=lambda r: r.priority)
    file = await comani.models.download(artifact, root=root, path=filename, reuse=True)
    phases.append({"phase": "model_download", "file": filename, "sha256": file.sha256,
                   "revision": source.revision, "seconds": time.perf_counter() - started})


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    with tempfile.TemporaryFile(dir=args.output_dir):
        pass
    print(f"Output directory: {args.output_dir}", flush=True)
    anchor_frames, trim, preserve = 56, 8, 39
    raw = max(5, round(args.duration * 24 + preserve - (anchor_frames - preserve)))
    bridge_frames = raw + (5 - raw % 17) % 17
    if bridge_frames <= 2 * preserve:
        raise ValueError("Duration must leave a generated middle after both protected windows")
    profile = {"workflow_resource": 3023857, "workflow_version": 3440094,
               "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "anchor_frames": anchor_frames, "trim": trim, "preserve": preserve,
               "bridge_frames": bridge_frames, "phases": []}
    started_all = time.perf_counter()
    try:
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            started = time.perf_counter()
            if args.install_plugins:
                installed = await comani.plugins.list()
                missing_plugins = [repository for repository in PLUGINS if not any(
                    Path(plugin.path).name.casefold() == repository.rsplit("/", 1)[-1].casefold()
                    or (plugin.repository or "").rstrip("/").removesuffix(".git").casefold()
                    == repository.casefold() for plugin in installed)]
                if missing_plugins:
                    await comani.comfyui.stop()
                    for repository in missing_plugins:
                        await comani.plugins.install_git(repository)
            info = await comani.comfyui.start()
            required = {"MiniMaxH3MaskedAVBridge", "VideoRollTrim", "AudioRollTrim", "AudioProcessor",
                        "ImageBatchExtendWithOverlap", "MiniMaxH3SigmaShift", "BlockSparseAttention"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Install original loop node packs first; missing: {sorted(missing)}")
            profile["phases"].append({"phase": "connect", "seconds": time.perf_counter() - started})
            weights = WEIGHTS if args.download_models else WEIGHTS[-1:] if args.download_lora else ()
            downloads = await asyncio.gather(
                *(acquire_model(*weight, profile["phases"]) for weight in weights), return_exceptions=True)
            if errors := [result for result in downloads if isinstance(result, BaseException)]:
                raise errors[0]
            started = time.perf_counter()
            width, height = comani.node("ResolutionSelector", aspect_ratio="4:3 (Standard)",
                                        megapixels=args.megapixels, multiple=32)
            model = comani.node("UNETLoader", unet_name="Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors",
                                weight_dtype="default")
            clip = comani.node("CLIPLoader", clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                               type="minimax", device="default")
            model, clip = comani.node("LoraLoader", model=model, clip=clip, lora_name=LORA,
                                      strength_model=1.0, strength_clip=1.0)
            model = comani.node("BlockSparseAttention", model=model, selection="sla", **{"selection.keep_percent": 10.0},
                                start_percent=0.2, end_percent=1.0, dense_blocks="", min_tokens=12288,
                                extra_tokens=256, sink_conditioning="exact_kv_and_rows", verbose=True)
            model = comani.node("ModelAttentionBackend", model=model, attention="comfy kitchen attention")
            model = comani.node("MiniMaxH3SigmaShift", model=model, shift_video=10.0, shift_audio=3.0)
            vae = comani.node("VAELoader", vae_name="minimax_h3_video_vae_int8_convrot.safetensors")
            audio_vae = comani.node("VAELoader", vae_name="minimax_h3_audio_vae_fp32.safetensors")
            sigmas = comani.node("BasicScheduler", model=model, scheduler="beta", steps=6, denoise=1.0)
            sampler = comani.node("KSamplerSelect", sampler_name="euler")
            await asyncio.gather(model, clip, vae, audio_vae, sigmas)
            profile["phases"].append({"phase": "load", "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            positive, latent = comani.node("MiniMaxH3ImageToVideo", clip=clip, vae=vae, prompt=args.prompt,
                                           width=width, height=height, length=anchor_frames)
            guider = comani.node("BasicGuider", model=model, conditioning=positive)
            noise = comani.node("RandomNoise", noise_seed=args.anchor_seed)
            sampled, _ = comani.node("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                                     sigmas=sigmas, latent_image=latent)
            anchor = comani.node("VAEDecode", samples=sampled, vae=vae)
            anchor_audio = comani.node("VAEDecodeAudio", samples=sampled, vae=audio_vae)
            anchor_trimmed, _, _ = comani.node("VideoRollTrim", images=anchor, offset=0, trim_start=trim, trim_end=trim)
            audio_trimmed, _, _ = comani.node("AudioRollTrim", audio=anchor_audio, offset=0,
                                             trim_start=trim / 24, trim_end=trim / 24)
            await asyncio.gather(anchor_trimmed, audio_trimmed)
            profile["phases"].append({"phase": "anchor_sample_decode", "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            positive, target = comani.node("MiniMaxH3ImageToVideo", clip=clip, vae=vae, prompt=args.prompt,
                                           width=width, height=height, length=bridge_frames)
            masked, _middle, _preserved = comani.node(
                "MiniMaxH3MaskedAVBridge", latent=target, vae=vae, audio_vae=audio_vae,
                start_frames=anchor_trimmed, start_audio=audio_trimmed,
                end_frames=anchor_trimmed, end_audio=audio_trimmed,
                start_fps=24.0, end_fps=24.0, preserve_frames=preserve, crop="disabled")
            guider = comani.node("BasicGuider", model=model, conditioning=positive)
            noise = comani.node("RandomNoise", noise_seed=args.bridge_seed)
            sampled, _ = comani.node("SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                                     sigmas=sigmas, latent_image=masked)
            bridge = comani.node("VAEDecode", samples=sampled, vae=vae)
            bridge_audio = comani.node("VAEDecodeAudio", samples=sampled, vae=audio_vae)
            await asyncio.gather(bridge, bridge_audio)
            profile["phases"].append({"phase": "bridge_encode_mask_sample_decode", "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            middle, _, tail = comani.node("VideoRollTrim", images=bridge, offset=0, trim_start=0, trim_end=preserve)
            _, _, blended = comani.node("ImageBatchExtendWithOverlap", source_images=tail,
                                        new_images=anchor_trimmed, overlap=preserve,
                                        overlap_side="source", overlap_mode="linear_blend")
            _, _, loop = comani.node("ImageBatchExtendWithOverlap", source_images=blended, new_images=middle,
                                     overlap=preserve, overlap_side="source", overlap_mode="linear_blend")
            _, _, count = comani.node("GetImageSize", image=loop)
            count = await (await count).fetch()
            bridge_center, _, _ = comani.node("AudioRollTrim", audio=bridge_audio, offset=0,
                                              trim_start=(preserve + trim / 2) / 24,
                                              trim_end=(preserve + trim / 2) / 24)
            anchor_rest, anchor_head, _ = comani.node("AudioRollTrim", audio=anchor_audio, offset=0,
                                                     trim_start=trim / 24, trim_end=0.0)
            anchor_rest, anchor_seconds = comani.node(
                "AudioProcessor", audio=anchor_rest, sample_rate=32000, volume=0.0, fade_in_time=0.0,
                fade_out_time=trim / 2 / 24, fade_curve="ease-outward", extend_start=0.0, extend_end=0.0)
            start_padding = await (await anchor_seconds).fetch() - trim / 2 / 24
            _duration, _, _, _, _ = comani.node("AudioInfo", audio=bridge_center)
            center_seconds = await (await _duration).fetch()
            center, center_seconds = comani.node(
                "AudioProcessor", audio=bridge_center, sample_rate=32000, volume=0.0,
                fade_in_time=trim / 2 / 24, fade_out_time=trim / 2 / 24, fade_curve="ease-outward",
                extend_start=start_padding, extend_end=count / 24 - center_seconds - start_padding)
            head_padding = await (await center_seconds).fetch() - trim / 24
            head, _ = comani.node("AudioProcessor", audio=anchor_head, sample_rate=32000, volume=0.0,
                                  fade_in_time=trim / 2 / 24, fade_out_time=0.0, fade_curve="ease-outward",
                                  extend_start=head_padding, extend_end=0.0)
            audio = comani.node("AudioMerge", audio1=center, audio2=anchor_rest, merge_method="add")
            audio = comani.node("AudioMerge", audio1=audio, audio2=head, merge_method="add")
            doubled = comani.node("ImageBatch", image1=loop, image2=loop)
            doubled_audio = comani.node("AudioConcat", audio1=audio, audio2=audio, direction="after")
            await asyncio.gather(audio, doubled_audio, doubled)
            profile["loop_frames"] = count
            profile["phases"].append({"phase": "av_stitch", "seconds": time.perf_counter() - started})
            outputs = {"loop": (loop, audio), "seam-check-x2": (doubled, doubled_audio)}
            if args.debug_videos:
                outputs.update(anchor=(anchor, anchor_audio), bridge=(bridge, bridge_audio))
            for name, (frames, audio) in outputs.items():
                video = comani.node("CreateVideo", images=frames, audio=audio, fps=24.0)
                saved = comani.node("SaveVideo", video=video, filename_prefix=f"comani/looper/{name}",
                                    format="mp4", codec="h264", return_call=True)
                file = await saved.file()
                started = time.perf_counter()
                target = args.output_dir / f"{name}.mp4"
                await comani.files.download(file, target)
                profile["phases"].append({"phase": "download", "name": name, "bytes": file.size,
                                          "seconds": time.perf_counter() - started})
                print(target, flush=True)
    except Exception as error:
        profile["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        profile["total_seconds"] = time.perf_counter() - started_all
        (args.output_dir / "profile.json").write_text(json.dumps(profile, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--prompt", default=PROMPT)
    parser.add_argument("--megapixels", type=float, default=0.7)
    parser.add_argument("--duration", type=float, default=7.0)
    parser.add_argument("--anchor-seed", type=int, default=843251249648163)
    parser.add_argument("--bridge-seed", type=int, default=80085)
    parser.add_argument("--install-plugins", action="store_true",
                        help="install missing original public node packages before starting the runtime")
    parser.add_argument("--download-models", action="store_true",
                        help="acquire all five original workflow weights through the daemon")
    parser.add_argument("--download-lora", action="store_true", help="acquire only Turbo-v4 when shared weights exist")
    parser.add_argument("--debug-videos", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path(".local/perfect-looper"))
    asyncio.run(main(parser.parse_args()))
