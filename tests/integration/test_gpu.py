"""Opt-in real daemon/GPU/model checks; no model download is performed."""

import asyncio
import gc
import json
import os
import time
from pathlib import Path

import pytest

from comani import Client

pytestmark = pytest.mark.gpu


@pytest.fixture
def gpu_url():
    url = os.environ.get("COMANI_TEST_URL")
    if not url:
        pytest.skip("Start the recorded GPU daemon and set COMANI_TEST_URL")
    return url


async def test_gpu_node_python_node_matches_native_function(gpu_url):
    import torch

    async with Client(gpu_url) as remote:
        source = remote.node("EmptyImage", width=64, height=64, batch_size=1, color=0x123456)
        adjusted = remote.python(lambda image: image.to("cuda") * 0.5,
                                 inputs={"image": source.output()})
        output = remote.node("ImageInvert", image=adjusted.output())
        reference = remote.python(
            lambda image: __import__("nodes").NODE_CLASS_MAPPINGS["ImageInvert"]().invert(
                image.to("cuda") * 0.5)[0], inputs={"image": source.output()},
        )
        actual = await (await output.output()).fetch()
        expected = await (await reference.output()).fetch()
        assert actual.device.type == "cuda"
        torch.testing.assert_close(actual, expected, rtol=0, atol=0)
        assert tuple(actual.shape) == (1, 64, 64, 3)


@pytest.mark.model
async def test_existing_checkpoint_eager_matches_native_workflow(gpu_url):
    import torch

    checkpoint = os.environ.get("COMANI_TEST_CHECKPOINT")
    if not checkpoint:
        pytest.skip("Set COMANI_TEST_CHECKPOINT to an existing local checkpoint filename")
    comfy = Path(os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI"))
    assert (comfy / "models/checkpoints" / checkpoint).is_file()
    record = Path(__file__).resolve().parents[2] / ".local/model-validation.json"
    record.parent.mkdir(exist_ok=True)

    def observe(stage, **details):
        daemon_record = record.parent / "daemon.json"
        if daemon_record.exists():
            daemon = json.loads(daemon_record.read_text())
            status = Path(f"/proc/{daemon['pid']}/status")
            if daemon.get("url") == gpu_url and status.exists():
                details["resources"] = {key: value.strip()
                                        for key, value in (line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
                                        if key in {"VmRSS", "VmSwap"}}
        snapshot = {"stage": stage, "time": time.time(), "checkpoint": checkpoint, **details}
        record.write_text(json.dumps(snapshot, indent=2) + "\n")
        with record.with_suffix(".jsonl").open("a") as history:
            history.write(json.dumps(snapshot) + "\n")

    async def eager_image(remote):
        loaded = remote.node("CheckpointLoaderSimple", ckpt_name=checkpoint)
        model, clip, vae = (loaded.output(i) for i in range(3))
        positive = remote.node("CLIPTextEncode", text="a red cube", clip=clip)
        negative = remote.node("CLIPTextEncode", text="", clip=clip)
        latent = remote.node("EmptyLatentImage", width=64, height=64, batch_size=1)
        adjusted = remote.python(lambda value: {"samples": value["samples"] + 0.0},
                                 inputs={"value": latent.output()})
        sampled = remote.node(
            "KSampler", model=model, seed=42, steps=1, cfg=1.0, sampler_name="euler",
            scheduler="normal", positive=positive.output(), negative=negative.output(),
            latent_image=adjusted.output(), denoise=1.0,
        )
        decoded = remote.node("VAEDecode", samples=sampled.output(), vae=vae)
        observe("eager_submitted", session=remote.session_id, call_id=decoded.id)
        eager = await (await decoded.output()).fetch()
        observe("eager_ready", session=remote.session_id, call_id=decoded.id)
        return eager

    async with asyncio.timeout(240), Client(gpu_url) as remote:
        eager = await eager_image(remote)
        # Only the host image survives the scope; do not hold two model sets.
        gc.collect()
        for _ in range(5):
            await asyncio.sleep(0)  # dispatch local finalizers, not a timing delay
            pending = [task for task in (remote._release_task, remote._forget_task) if task]
            if not pending:
                break
            await asyncio.gather(*pending)
        assert not remote._pending_releases
        assert not remote._pending_forgets
        observe("eager_released", session=remote.session_id, release_forget_acked=True)
        prompt = {
            "loader": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red cube", "clip": ["loader", 1]}},
            "negative": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["loader", 1]}},
            "latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}},
            "sample": {"class_type": "KSampler", "inputs": {
                "model": ["loader", 0], "seed": 42, "steps": 1, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "normal", "positive": ["positive", 0],
                "negative": ["negative", 0], "latent_image": ["latent", 0], "denoise": 1.0,
            }},
            "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["loader", 2]}},
        }
        run = remote.submit(prompt, outputs={"image": ["decode", 0]}, targets=["decode"])
        observe("native_submitted", session=remote.session_id, call_id=run.id)
        # Explicit eager consumption protects only this accepted input, then
        # owns its returned image; the workflow cache remains free to evict.
        retained = remote.python(lambda image: image, inputs={"image": run.output("image")})
        await retained._state.accepted
        observe("native_consumer_accepted", session=remote.session_id,
                call_id=run.id, consumer_id=retained.id)
        native = await (await retained.output()).fetch()
        assert tuple(eager.shape) == (1, 64, 64, 3)
        assert torch.isfinite(eager).all()
        torch.testing.assert_close(eager, native, rtol=1e-4, atol=1e-4)
        observe("passed", session=remote.session_id, call_id=run.id, consumer_id=retained.id,
                shape=list(eager.shape), max_abs_error=float((eager - native).abs().max()))
