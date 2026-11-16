"""Render three video previews, choose one, then upscale its remote AV latent.

Eager adaptation of foxydits's Seed Hunter v3 (https://civitai.com/models/2881362).
Uses the original Singularity v1.3 INT8, NVFP4 Qwen encoder, Kijai INT8 VAE,
Silver DARE-TIES v2 LoRA at 0.75, beta/er_sde previews and latent refinement.
Defaults follow the source's connected sliders: 6 steps, 0.5 MP, 9:16, 4 seconds
(107 aligned frames), followed by 1.5 MP refinement. Widths use the core's
1024*1024 pixels per megapixel and nearest-multiple-of-32 rule.
Requires a GPU/runtime supporting those quantizations; validate that capability.
Models, LoRA and outputs retain their upstream license restrictions.

python examples/h3_seed_hunter.py --prompt "A red fox explores a snowy garden..." \
    --output-dir .local/seed-hunter --choose 1
Without --choose, all three previews are generated before you select one.
--choose 2 3 generates only candidates2/3, then refines both in the same session.
Outputs default to a private local directory because H3 has territorial restrictions.
--output-dir is a root; each invocation creates and prints a new run-* subdirectory.
"""

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

import comani

MODEL = "Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors"
CLIP = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_int8_convrot.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"
LORA = "minimax_h3_ref2v_turbo_silver_dareties_comfy_pruned_v2.safetensors"
UPSCALER = "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"
WEIGHTS = (
    ("diffusion_models", "WarmBloodAban/Minimax-h3_Singularity", MODEL),
    ("text_encoders", "Comfy-Org/MiniMax-H3", "text_encoders/" + CLIP),
    ("vae", "Kijai/MiniMax-H3-experimental", VIDEO_VAE),
    ("vae", "Comfy-Org/MiniMax-H3", "vae/" + AUDIO_VAE),
    ("loras", "silveroxides/MiniMax-H3_tests", "silver_official_releases/" + LORA),
    ("latent_upscale_models", "LBH-123-AI/Minimax_h3_latent_Upscaler",
     "minimax_h3_latent_upscaler_3d_conv_v1/" + UPSCALER),
)


async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    print(f"Output directory: {args.output_dir}", flush=True)
    async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
        info = await comani.comfyui.start()
        required = {"MiniMaxH3ReferenceToVideo", "ModelAttentionBackend", "BlockSparseAttention",
                    "MiniMaxH3LatentUpscaleWithModel", "LatentUpscaleModelLoader",
                    "SamplerCustomAdvanced", "CreateVideo", "SaveVideo"}
        if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
            raise ValueError(f"Missing backend nodes: {sorted(missing)}")
        if args.download_models:
            sources = await asyncio.gather(*(comani.models.resolve({"repo_id": repo, "path": path})
                                             for _, repo, path in WEIGHTS))
            downloads = await asyncio.gather(*(comani.models.download(
                source.files[0], category=category, path=Path(path).name, reuse=True)
                for (category, _, path), source in zip(WEIGHTS, sources)), return_exceptions=True)
            for result in downloads:
                if isinstance(result, BaseException):
                    raise result

        # Fixed preparation is shared by all preview seeds and selected refinements.
        model = comani.node("UNETLoader", unet_name=MODEL, weight_dtype="default")
        model = comani.node("ModelAttentionBackend", model=model, attention="comfy kitchen attention")
        model = comani.node("LoraLoaderModelOnly", model=model, lora_name=LORA, strength_model=0.75)
        clip = comani.node("CLIPLoader", clip_name=CLIP, type="minimax", device="default")
        video_vae = comani.node("VAELoader", vae_name=VIDEO_VAE)
        audio_vae = comani.node("VAELoader", vae_name=AUDIO_VAE)
        positive, empty_latent = comani.node(
            "MiniMaxH3ReferenceToVideo", clip=clip, vae=video_vae, audio_vae=audio_vae,
            prompt=args.prompt, width=args.preview_width, height=args.preview_height,
            length=args.frames, ref_image_size="max")
        guider = comani.node("BasicGuider", model=model, conditioning=positive)
        sampler = comani.node("KSamplerSelect", sampler_name="er_sde")
        sigmas = comani.node("BasicScheduler", model=model, scheduler="beta", steps=args.steps, denoise=1.0)

        candidates = {}
        for index in args.choose or (1, 2, 3):
            seed = args.seed + (index - 1) * 1000
            noise = comani.node("RandomNoise", noise_seed=seed)
            _sampled, denoised = comani.node(
                "SamplerCustomAdvanced", noise=noise, guider=guider, sampler=sampler,
                sigmas=sigmas, latent_image=empty_latent)
            # Keep the original AV latent together; the downloaded MP4 is only a preview.
            candidates[index] = await denoised
            frames = comani.node("VAEDecode", samples=candidates[index], vae=video_vae)
            audio = comani.node("VAEDecodeAudio", samples=candidates[index], vae=audio_vae)
            video = comani.node("CreateVideo", images=frames, audio=audio, fps=24.0)
            saved = comani.node("SaveVideo", video=video, filename_prefix="comani/seed_hunter/preview",
                                format="mp4", codec="h264", return_call=True)
            target = args.output_dir / f"preview-{index}-seed-{seed}.mp4"
            await comani.files.download(await saved.file(), target)
            print(target, flush=True)

        choices = args.choose
        if choices is None:
            choices = [int(await asyncio.to_thread(input, "Choose a preview to refine (1, 2, or 3): "))]
        if any(choice not in candidates for choice in choices):
            raise ValueError("Choose a generated candidate: 1, 2, or 3")

        # This fixed second-pass branch is prepared once after the choice.
        upscaler = comani.node("LatentUpscaleModelLoader", model_name=UPSCALER)
        refine_model = comani.node(
            "BlockSparseAttention", model=model, selection="sla", **{"selection.keep_percent": 10.0},
            start_percent=0.2, end_percent=1.0, dense_blocks="", min_tokens=12288, extra_tokens=256,
            sink_conditioning="exact_kv_and_rows", verbose=False)
        refine_guider = comani.node("BasicGuider", model=refine_model, conditioning=positive)
        refine_sigmas = comani.node("BasicScheduler", model=refine_model, scheduler="linear_quadratic",
                                    steps=args.refine_steps, denoise=0.35)
        for choose in choices:
            enlarged = comani.node("MiniMaxH3LatentUpscaleWithModel", model=upscaler, samples=candidates[choose],
                                   width=args.width, height=args.height)
            noise = comani.node("RandomNoise", noise_seed=args.refine_seed)
            refined, _ = comani.node("SamplerCustomAdvanced", noise=noise, guider=refine_guider,
                                     sampler=sampler, sigmas=refine_sigmas, latent_image=enlarged)
            frames = comani.node("VAEDecode", samples=refined, vae=video_vae)
            audio = comani.node("VAEDecodeAudio", samples=refined, vae=audio_vae)
            video = comani.node("CreateVideo", images=frames, audio=audio, fps=24.0)
            saved = comani.node("SaveVideo", video=video, filename_prefix=f"comani/seed_hunter/final-{choose}",
                                format="mp4", codec="h264", return_call=True)
            target = args.output_dir / f"final-candidate-{choose}.mp4"
            await comani.files.download(await saved.file(), target)
            print(target, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMANI_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--seed", type=int, default=770942841899791)
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--frames", type=int, default=107, help="24fps, rounded up to 17n+5")
    parser.add_argument("--preview-width", type=int, default=544)
    parser.add_argument("--preview-height", type=int, default=960)
    parser.add_argument("--width", type=int, default=928)
    parser.add_argument("--height", type=int, default=1664)
    parser.add_argument("--refine-steps", type=int, default=2)
    parser.add_argument("--refine-seed", type=int, default=457810346125114)
    parser.add_argument("--choose", type=int, nargs="+", choices=(1, 2, 3),
                        help="known candidate(s) to generate and refine; omit to preview all three first")
    parser.add_argument("--output-dir", type=Path, default=Path(".local/h3-seed-hunter"))
    parser.add_argument("--download-models", action="store_true")
    args = parser.parse_args()
    if args.choose and len(set(args.choose)) != len(args.choose):
        parser.error("--choose candidate numbers must be distinct")
    asyncio.run(main(args))
