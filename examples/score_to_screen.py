"""Make two scored scenes through the original native dynamic scene graph.

ScoreToScreen v9.0/resource2975051/version3470191. Requires the four original
original Score packages, Python3.12, official4B Q4_K_M GGUF+F16 mmproj,
local llama-server, original FastH3 Omni NVFP4 and the shared H3 components.
The default LLM uses CPU; --llm-device cuda selects the original CUDA build
and all GPU layers with the same GGUF and vision weights.
PromptSplitterLLM remains real: its local server sees the picture and exits
after splitting. No external paid API or YuE2 music generation is used.
The input audio/score/images are supplied by the caller. The default8-step
FastH3 engine, shifts12/3 and VSA10% match the supplied GUI; size/duration are
small explicit validation inputs. Models/outputs retain their original terms.

python examples/score_to_screen.py --image adult.png --audio song.wav --abc song.abc
The official node archive is declared below; --install-plugins permits its
daemon-side acquisition/installation, which still needs remote validation.
Add --prepare-llama --download-models to prepare
tools and weights. The original node chooses a compatible
official llama.cpp release only when explicitly permitted; no release is pinned.
For a CPU-limited server, --llm-threads N passes -t N -tb N to the original
llama.cpp node. Choose N from that server's available CPU quota; omitting it
keeps the upstream default. This setting does not change H3 sampling.
The scene runner's lazy ports are graph templates, so this uses comani.graph
and submit, preserving hidden PROMPT links and native dynamic expansion.
"""

import argparse
import asyncio
import json
import os
import shlex
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import comani

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


async def acquire_model(category, repository, path, filename, phases):
    started = time.perf_counter()
    source = await comani.models.resolve({"repo_id": repository, "path": path})
    artifact, = source.files
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
    profile = {"workflow_resource": 2975051, "workflow_version": 3470191,
               "engine": "fasth3_omni", "llm": f"original4B GGUF Q4_K_M with F16vision, {args.llm_device}",
               "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "phases": []}
    started_all = time.perf_counter()
    try:
        async with comani.Client(args.url, token=os.environ.get("COMANI_TOKEN")):
            started = time.perf_counter()
            configured = await comani.comfyui.info()
            await comani.bash.run(shlex.join([configured.runtime_python, "-c",
                "import sys; assert sys.version_info >= (3,12), 'Original Score packages require Python >=3.12'"]))
            if args.install_plugins:
                installed = await comani.plugins.list()
                missing = {name: path for name, path in NODE_ARCHIVE["packages"].items() if not any(
                    Path(plugin.path).name.casefold() == name.casefold() for plugin in installed)}
                if missing:
                    await comani.comfyui.stop()
                    profile["node_installation"] = asdict(await comani.plugins.install_archive(
                        {**NODE_ARCHIVE, "packages": missing}))
            info = await comani.comfyui.start()
            required = {"ABCLoadSong", "S2SSceneList", "S2SScenePlanN", "S2SSceneRunner", "S2SLLMSettings",
                        "PromptSplitterLLM", "S2SH3Engine", "S2SOmniPatch", "ABCTimingPrompt"}
            if missing := required - info.extra.get("node_metadata", {}).get("nodes", {}).keys():
                raise ValueError(f"Install original Score packages first; missing: {sorted(missing)}")
            # Enumerating this original node registers its LLM model category.
            # Preflight the original tool before model loading or scene execution.
            ready = await comani.python(code="""def main(prepare, device):
    import hashlib, importlib, nodes, re, subprocess
    nodes.NODE_CLASS_MAPPINGS['S2SLLMSettings'].INPUT_TYPES()
    module = importlib.import_module(nodes.NODE_CLASS_MAPPINGS['S2SLLMSettings'].__module__)
    gguf = importlib.import_module(module.__package__ + '.gguf_server')
    errors = []
    for variant, executable in gguf.candidates(device):
        try:
            if not executable and prepare:
                executable = gguf.download_build(variant)
            if not executable:
                raise ValueError('Missing original tool; use --prepare-llama to prepare it')
            check = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=30, check=True)
            devices = ''
            if device == 'cuda':
                check_devices = subprocess.run([executable, '--list-devices'], capture_output=True,
                                               text=True, timeout=30, check=True)
                devices = check_devices.stdout + check_devices.stderr
                if not re.search(r'CUDA\\d+:', devices):
                    raise RuntimeError('Original CUDA tool did not enumerate a CUDA device: ' + devices)
            with open(executable, 'rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            return {'path': executable, 'variant': variant, 'sha256': checksum,
                    'version': check.stdout + check.stderr, 'devices': devices}
        except Exception as error:
            errors.append(f'{variant}: {type(error).__name__}: {error}')
    raise RuntimeError('Original llama.cpp preflight failed: ' + '; '.join(errors))
""", entry="main", inputs={"prepare": args.prepare_llama, "device": args.llm_device})
            profile["llama_tool"] = await ready.fetch()
            if args.download_models:
                downloads = await asyncio.gather(
                    *(acquire_model(*weight, profile["phases"]) for weight in WEIGHTS), return_exceptions=True)
                if errors := [result for result in downloads if isinstance(result, BaseException)]:
                    raise errors[0]
            profile["phases"].append({"phase": "connect", "seconds": time.perf_counter() - started})
            started = time.perf_counter()
            folder = "score-" + uuid.uuid4().hex[:12]
            image, song, score = await asyncio.gather(
                comani.files.upload(args.image, path=f"{folder}/person.png"),
                comani.files.upload(args.audio, path=f"{folder}/song.wav"),
                comani.files.upload(args.abc, path=f"{folder}/song.abc"))
            profile["input_files"] = [{"native_name": f.native_name, "bytes": f.size, "sha256": f.sha256}
                                      for f in (image, song, score)]
            profile["phases"].append({"phase": "upload", "seconds": time.perf_counter() - started})
            scenes = json.dumps([
                {"image": image.native_name, "text": args.scene1, "start": "own picture", "sound": "song"},
                {"image": image.native_name, "text": args.scene2, "start": "continue", "sound": "silent"}])
            with comani.graph() as workflow:
                audio, abc, _song_report, _selected, start = comani.node(
                    "ABCLoadSong", song=f"input/{song.relative_path}", start="0")
                scene_list, _count, picture = comani.node("S2SSceneList", scenes=scenes,
                                                        missing_picture="use scene 1's picture")
                plan, _total, _plan_report, _first_seconds, first_frames, rest_frames = comani.node(
                    "S2SScenePlanN", scenes=scene_list, start_sec=start, max_scene_sec=args.scene_seconds,
                    whole_song=True, song=audio, abc=abc, song_delay=0.0)
                mixed, _sfx, _mix_report = comani.node(
                    "SongSfxMix", song=audio, enabled=False, s1_frames=first_frames, s2_frames=rest_frames,
                    fps=24, sfx_volume=0.35, song_volume=1.0, remove_vocals=True, fade_ms=20,
                    song_volume_curve="", normalize_loudness=True, target_lufs=-16.0)
                llm = comani.node(
                    "S2SLLMSettings", backend="gguf", gguf_model=args.llm_model, mmproj=args.mmproj,
                    gpu_layers="all" if args.llm_device == "cuda" else "cpu",
                    llama_build=args.llm_device, ctx_size=8192,
                    extra_args=f"-t {args.llm_threads} -tb {args.llm_threads}" if args.llm_threads else "",
                    hf_model="Qwen3-VL-4B-Instruct (download, about 9 GB, recommended)",
                    quantization="none", device="cpu", lm_studio_url="http://127.0.0.1:1234/v1", lm_studio_model="")
                # This is the original shared prompt template. The runner reads its
                # hidden graph inputs, then creates one real local-LLM call per scene.
                template = comani.node(
                    "PromptSplitterLLM", prompt="An adult visitor explores a peaceful garden. No singing.",
                    duration=args.scene_seconds, base_url="http://127.0.0.1:1234/v1", model="", api_key="",
                    temperature=0.3, seed=0, timeout=600, instrumental_mode="instrumental",
                    instrumental_audio="yue2", unload_after=True, prompt_style="shot_list",
                    use_image="auto", detail_level="minimal", sfx_only=False, min_first_shot=3.0,
                    thinking="off", prompt_format="ref2va_structured", llm=llm, return_call=True)
                _name, turbo, steps, sampler, _sampler_name, omni = comani.node(
                    "S2SH3Engine", engine="fasth3_omni", omni_model=args.model, omni_steps=8,
                    h3_sampler="res_multistep", omni_sampler="euler", standard_steps=20)
                model = comani.node("UNETLoader", unet_name=args.model, weight_dtype="default")
                model = comani.node("ModelAttentionBackend", model=model, attention="comfy kitchen attention")
                model = comani.node("S2SOmniPatch", model=model, omni=omni, shift_video=12.0, shift_audio=3.0,
                                    vsa=True, keep_percent=10.0, start_percent=0.0, end_percent=1.0,
                                    min_tokens=12288, extra_tokens=256, sink_conditioning="exact_kv_and_rows",
                                    vsa_log=True)
                clip = comani.node("CLIPLoader", clip_name="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                                   type="minimax", device="default")
                vae = comani.node("VAELoader", vae_name="minimax_h3_video_vae_int8_convrot.safetensors")
                audio_vae = comani.node("VAELoader", vae_name="minimax_h3_audio_vae_fp32.safetensors")
                # These primitive producers keep required lazy ports as real links.
                seed = comani.node("PrimitiveInt", value=args.seed)
                width = comani.node("PrimitiveInt", value=args.width)
                height = comani.node("PrimitiveInt", value=args.height)
                turbo_steps = comani.node("PrimitiveInt", value=4)
                yes = comani.node("PrimitiveBoolean", value=True)
                no = comani.node("PrimitiveBoolean", value=False)
                off = comani.node("PrimitiveString", value="off")
                result = comani.node(
                    "S2SSceneRunner", scenes=scene_list, plan=plan, mode="off", scale=1.5,
                    quality="ULTRA", strength=1.0, review="auto", handoff="1 frame",
                    scene_brief="director + scene before", decode="full", song_db=0.0, h3_db=0.0,
                    blend_db=-14.0, video_crf=18, audio_kbps=256, vae=vae, audio_vae=audio_vae, clip=clip,
                    model=model, sampler=sampler, use_turbo=turbo, steps_off=steps, turbo_steps=turbo_steps,
                    seed_random=seed, seed_lock=yes, seed_fixed=seed, width=width, height=height,
                    song=audio, abc=abc, instrumental=yes, audio_lock=off, song_delay=comani.node("PrimitiveFloat", value=0.0),
                    min_first_shot=comani.node("PrimitiveFloat", value=3.0), band_cues=yes,
                    drum_audio_sync=no, strum_ai=no, strum_keys=no, strum_fallback=off,
                    on_screen_singing=comani.node("PrimitiveString", value="no"), llm=llm,
                    song_llm=template.output(0), fallback_picture=picture, mv_audio=mixed,
                    return_call=True)
                saved = comani.node("SaveVideo", video=result.output(0), filename_prefix="comani/score-to-screen",
                                    format="mp4", codec="h264", return_call=True)
                workflow.output("report", result.output(1))
                workflow.output("prompts", result.output(2))
                workflow.output("plan", plan)
                workflow.target(saved)
            started = time.perf_counter()
            run = comani.submit(workflow, return_call=True)
            metadata = await run.metadata()
            file = await run.file(node_id=saved.id)
            profile["phases"].append({"phase": "native_graph_llm_sample_decode_join",
                                      "seconds": time.perf_counter() - started})
            profile["call_id"], profile["prompt_id"] = run.id, metadata.prompt_id
            for index, name in enumerate(("report", "prompts", "plan")):
                (args.output_dir / f"{name}.txt").write_text(await (await run.output(index)).fetch())
            started = time.perf_counter()
            target = args.output_dir / "film.mp4"
            await comani.files.download(file, target)
            profile["phases"].append({"phase": "download", "bytes": file.size,
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
