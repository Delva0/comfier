"""Make two scored scenes with the original ScoreToScreen v9.0 dynamic graph.

Source: https://civitai.com/models/2975051 (version 3470191).
Requires Python 3.12, the four original Score packages, a compatible FastH3
Omni NVFP4 runtime and the GGUF/F16 vision weights declared below.
The input picture, audio and ABC score are supplied by the caller.
Models, plugins and outputs retain their upstream terms.

python examples/score_to_screen.py --image adult.png --audio song.wav --abc song.abc \
    --install-plugins --prepare-llama --download-models
Omit the preparation flags on subsequent runs. The daemon obtains the public
node archive and weights directly. --prepare-llama permits the original node
to select an official llama.cpp release; no release is hardcoded here.
CPU is the default; --llm-threads uses the server's available CPU quota.
CUDA needs compatible driver/runtime libraries; this example does not repair
third-party tool installations. A complete Score film is still unverified.
The graph preserves the original lazy templates, hidden PROMPT links and real
per-scene LLM calls. Uploaded inputs remain because native scenes refer to them.
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

PACKAGES = ("ComfyUI-ABCScoreIO", "ComfyUI-PromptSplitter", "ComfyUI-ABCTiming", "ComfyUI-ABCInstrumentalize")
NODE_ARCHIVE = {
    "url": "https://civitai.com/api/download/models/3470191?fileId=3377291",
    "sha256": "c6f55583038e259c441cebe8a0e31e9cfc727f9f4990e1dc65ed83488dad4b49",
    "size": 2056796,
    "packages": {name: "Score2Screen_v9.0/custom_nodes/" + name for name in PACKAGES},
}
WEIGHTS = (
    ("diffusion_models", "FastVideo/FastH3-Omni-NVFP4-QAD-Comfy", "fasth3_omniref_qad_nvfp4_comfy.safetensors", "fasth3_omniref_qad_nvfp4_comfy.safetensors"),
    ("text_encoders", "Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    ("vae", "Kijai/MiniMax-H3-experimental", "minimax_h3_video_vae_int8_convrot.safetensors", "minimax_h3_video_vae_int8_convrot.safetensors"),
    ("vae", "Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", "minimax_h3_audio_vae_fp32.safetensors"),
    ("LLM", "Qwen/Qwen3-VL-4B-Instruct-GGUF", "Qwen3VL-4B-Instruct-Q4_K_M.gguf", "Qwen3-VL-4B-Instruct-GGUF/Qwen3VL-4B-Instruct-Q4_K_M.gguf"),
    ("LLM", "Qwen/Qwen3-VL-4B-Instruct-GGUF", "mmproj-Qwen3VL-4B-Instruct-F16.gguf", "Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-F16.gguf"),
)

async def main(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir = Path(tempfile.mkdtemp(prefix="run-", dir=args.output_dir)).resolve()
    print(f"Output directory: {args.output_dir}", flush=True)
    async with cf.Client(args.url, token=os.environ.get("COMFIER_TOKEN")):
        configured = await cf.comfyui.info()
        await cf.bash.run(shlex.join([configured.runtime_python, "-c",
            "import sys; assert sys.version_info >= (3,12), 'Original Score packages require Python >=3.12'"]))
        if args.install_plugins:
            installed = await cf.plugins.list()
            missing = {name: path for name, path in NODE_ARCHIVE["packages"].items() if not any(
                Path(plugin.path).name.casefold() == name.casefold() for plugin in installed)}
            if missing:
                await cf.comfyui.stop()
                await cf.plugins.install_archive({**NODE_ARCHIVE, "packages": missing})
        info = await cf.comfyui.start()
        required = {"ABCLoadSong", "S2SSceneList", "S2SScenePlanN", "S2SSceneRunner", "S2SLLMSettings",
                    "PromptSplitterLLM", "S2SH3Engine", "S2SOmniPatch", "ABCTimingPrompt"}
        if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
            raise ValueError(f"Install original Score packages first; missing: {sorted(missing)}")
        # INPUT_TYPES registers the original LLM model directory. Tool selection
        # and downloads remain with the node package; no vendor cache repair here.
        await cf.python(code=r"""def prepare(allow_download, device):
    import importlib, nodes, re, subprocess
    settings = nodes.NODE_CLASS_MAPPINGS['S2SLLMSettings']
    settings.INPUT_TYPES()
    module = importlib.import_module(settings.__module__)
    gguf = importlib.import_module(module.__package__ + '.gguf_server')
    errors = []
    for variant, executable in gguf.candidates(device):
        try:
            if not executable and allow_download:
                executable = gguf.download_build(variant)
            if not executable:
                raise RuntimeError('Missing llama-server; add --prepare-llama')
            option = '--list-devices' if device == 'cuda' else '--version'
            check = subprocess.run([executable, option], capture_output=True, text=True, timeout=30, check=True)
            if device == 'cuda' and not re.search(r'CUDA\d+:', check.stdout + check.stderr):
                raise RuntimeError('The original CUDA tool did not enumerate a CUDA device')
            return variant
        except Exception as error:
            errors.append(f'{variant}: {error}')
    raise RuntimeError('Original llama.cpp preparation failed: ' + '; '.join(errors))
""", entry="prepare", inputs={"allow_download": args.prepare_llama, "device": args.llm_device})
        if args.download_models:
            sources = await asyncio.gather(*(cf.models.resolve({"repo_id": repo, "path": path})
                                             for _, repo, path, _ in WEIGHTS))
            downloads = await asyncio.gather(*(cf.models.download(
                source.files[0], category=category, path=filename, reuse=True)
                for (category, _, _, filename), source in zip(WEIGHTS, sources)), return_exceptions=True)
            for result in downloads:
                if isinstance(result, BaseException):
                    raise result
        folder = "score-" + uuid.uuid4().hex[:12]
        image, song, _score = await asyncio.gather(
            cf.files.upload(args.image, path=f"{folder}/person.png"),
            cf.files.upload(args.audio, path=f"{folder}/song.wav"),
            cf.files.upload(args.abc, path=f"{folder}/song.abc"))
        scenes = json.dumps([
            {"image": image.native_name, "text": args.scene1, "start": "own picture", "sound": "song"},
            {"image": image.native_name, "text": args.scene2, "start": "continue", "sound": "silent"}])
        with cf.graph() as workflow:
            audio, abc, _song_report, _selected, start = cf.node(
                "ABCLoadSong", song=f"input/{song.relative_path}", start="0")
            scene_list, _count, picture = cf.node("S2SSceneList", scenes=scenes,
                                                    missing_picture="use scene 1's picture")
            plan, _total, _plan_report, _first_seconds, first_frames, rest_frames = cf.node(
                "S2SScenePlanN", scenes=scene_list, start_sec=start, max_scene_sec=args.scene_seconds,
                whole_song=True, song=audio, abc=abc, song_delay=0.0)
            mixed, _sfx, _mix_report = cf.node(
                "SongSfxMix", song=audio, enabled=False, s1_frames=first_frames, s2_frames=rest_frames,
                fps=24, sfx_volume=0.35, song_volume=1.0, remove_vocals=True, fade_ms=20,
                song_volume_curve="", normalize_loudness=True, target_lufs=-16.0)
            llm = cf.node(
                "S2SLLMSettings", backend="gguf", gguf_model=args.llm_model, mmproj=args.mmproj,
                gpu_layers="all" if args.llm_device == "cuda" else "cpu",
                llama_build=args.llm_device, ctx_size=8192,
                extra_args=f"-t {args.llm_threads} -tb {args.llm_threads}" if args.llm_threads else "",
                hf_model="Qwen3-VL-4B-Instruct (download, about 9 GB, recommended)",
                quantization="none", device="cpu", lm_studio_url="http://127.0.0.1:1234/v1", lm_studio_model="")
            # This is the original shared prompt template. The runner reads its
            # hidden graph inputs, then creates one real local-LLM call per scene.
            template = cf.node(
                "PromptSplitterLLM", prompt="An adult visitor explores a peaceful garden. No singing.",
                duration=args.scene_seconds, base_url="http://127.0.0.1:1234/v1", model="", api_key="",
                temperature=0.3, seed=0, timeout=600, instrumental_mode="instrumental",
                instrumental_audio="yue2", unload_after=True, prompt_style="shot_list",
                use_image="auto", detail_level="minimal", sfx_only=False, min_first_shot=3.0,
                thinking="off", prompt_format="ref2va_structured", llm=llm, return_call=True)
            _name, turbo, steps, sampler, _sampler_name, omni = cf.node(
                "S2SH3Engine", engine="fasth3_omni", omni_model=args.model, omni_steps=8,
                h3_sampler="res_multistep", omni_sampler="euler", standard_steps=20)
            model = cf.node("UNETLoader", unet_name=args.model, weight_dtype="default")
            model = cf.node("ModelAttentionBackend", model=model, attention="comfy kitchen attention")
            model = cf.node("S2SOmniPatch", model=model, omni=omni, shift_video=12.0, shift_audio=3.0,
                                vsa=True, keep_percent=10.0, start_percent=0.0, end_percent=1.0,
                                min_tokens=12288, extra_tokens=256, sink_conditioning="exact_kv_and_rows",
                                vsa_log=True)
            clip = cf.node("CLIPLoader", clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                               type="minimax", device="default")
            vae = cf.node("VAELoader", vae_name="minimax_h3_video_vae_int8_convrot.safetensors")
            audio_vae = cf.node("VAELoader", vae_name="minimax_h3_audio_vae_fp32.safetensors")
            # These primitive producers keep required lazy ports as real links.
            seed = cf.node("PrimitiveInt", value=args.seed)
            width = cf.node("PrimitiveInt", value=args.width)
            height = cf.node("PrimitiveInt", value=args.height)
            turbo_steps = cf.node("PrimitiveInt", value=4)
            yes = cf.node("PrimitiveBoolean", value=True)
            no = cf.node("PrimitiveBoolean", value=False)
            off = cf.node("PrimitiveString", value="off")
            result = cf.node(
                "S2SSceneRunner", scenes=scene_list, plan=plan, mode="off", scale=1.5,
                quality="ULTRA", strength=1.0, review="auto", handoff="1 frame",
                scene_brief="director + scene before", decode="full", song_db=0.0, h3_db=0.0,
                blend_db=-14.0, video_crf=18, audio_kbps=256, vae=vae, audio_vae=audio_vae, clip=clip,
                model=model, sampler=sampler, use_turbo=turbo, steps_off=steps, turbo_steps=turbo_steps,
                seed_random=seed, seed_lock=yes, seed_fixed=seed, width=width, height=height,
                song=audio, abc=abc, instrumental=yes, audio_lock=off, song_delay=cf.node("PrimitiveFloat", value=0.0),
                min_first_shot=cf.node("PrimitiveFloat", value=3.0), band_cues=yes,
                drum_audio_sync=no, strum_ai=no, strum_keys=no, strum_fallback=off,
                on_screen_singing=cf.node("PrimitiveString", value="no"), llm=llm,
                song_llm=template.output(0), fallback_picture=picture, mv_audio=mixed,
                return_call=True)
            saved = cf.node("SaveVideo", video=result.output(0), filename_prefix="comfier/score-to-screen",
                                format="mp4", codec="h264", return_call=True)
            workflow.output("report", result.output(1))
            workflow.output("prompts", result.output(2))
            workflow.output("plan", plan)
            workflow.target(saved)
        run = cf.submit(workflow, return_call=True)
        file = await run.file(node_id=saved.id)
        for index, name in enumerate(("report", "prompts", "plan")):
            (args.output_dir / f"{name}.txt").write_text(await (await run.output(index)).fetch())
        target = args.output_dir / "film.mp4"
        await cf.files.download(file, target)
        print(target, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("COMFIER_URL", "http://127.0.0.1:8190"))
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--abc", type=Path, required=True)
    parser.add_argument("--install-plugins", action="store_true",
                        help="install missing original Score packages from the official archive on the daemon")
    parser.add_argument("--prepare-llama", action="store_true",
                        help="permit the original package to prepare its compatible official llama-server")
    parser.add_argument("--llm-device", choices=("cpu", "cuda"), default="cpu",
                        help="original GGUF runtime; cuda uses the native CUDA build and all GPU layers")
    parser.add_argument("--download-models", action="store_true",
                        help="acquire the default FastH3, shared H3, GGUF and vision weights through the daemon")
    parser.add_argument("--scene1", default="An adult visitor looks around a peaceful garden. No singing or instruments.")
    parser.add_argument("--scene2", default="The same adult visitor walks beside green plants. Silent scene, no dialogue.")
    parser.add_argument("--width", type=int, default=544)
    parser.add_argument("--height", type=int, default=960)
    parser.add_argument("--scene-seconds", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=201)
    parser.add_argument("--model", default="fasth3_omniref_qad_nvfp4_comfy.safetensors")
    parser.add_argument("--llm-model", default="Qwen3-VL-4B-Instruct-GGUF/Qwen3VL-4B-Instruct-Q4_K_M.gguf")
    parser.add_argument("--mmproj", default="Qwen3-VL-4B-Instruct-GGUF/mmproj-Qwen3VL-4B-Instruct-F16.gguf")
    parser.add_argument("--llm-threads", type=int,
                        help="CPU llama.cpp inference/batch threads; omit for the original node default")
    parser.add_argument("--output-dir", type=Path, default=Path(".local/score-to-screen"))
    args = parser.parse_args()
    if args.llm_threads is not None and args.llm_threads < 1:
        parser.error("--llm-threads must be positive")
    if args.download_models and (args.model, args.llm_model, args.mmproj) != (
        WEIGHTS[0][3], WEIGHTS[4][3], WEIGHTS[5][3]
    ):
        parser.error("--download-models prepares the default weight set; preinstall custom selections")
    asyncio.run(main(args))
