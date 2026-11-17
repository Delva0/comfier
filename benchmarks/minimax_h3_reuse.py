"""Benchmark H3 ref reuse and overlap generation with Tensor fetch/save.

Uses the same explicit environment/model preparation arguments as
minimax_h3_remote.py. Tensor fetch/save is intentional in this benchmark and
requires local Torch/NumPy/Pillow. --serial keeps the original eager seed path
without compute/fetch overlap. Historical measurements retain their old hashes.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import comfier as cf

# Direct execution also finds the shared, editable H3 preparation example.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import minimax_h3_remote as h3  # noqa: E402


@dataclass
class Setup:
    model: cf.RemoteRef
    positive: cf.RemoteRef
    latent: cf.RemoteRef
    negative: cf.RemoteRef
    vae: cf.RemoteRef

    def release(self):
        for ref in (self.model, self.positive, self.latent, self.negative, self.vae):
            ref.release()


async def setup(remote, args):
    """Keep unchanged inputs alive; native ComfyUI still controls VRAM/offload."""
    clip = remote.node('CLIPLoader', clip_name=h3.ENCODER, type='minimax', device='default')
    condition = remote.node('MiniMaxH3ImageConditioning', clip=clip,
                            prompt=args.prompt, width=args.width, height=args.height)
    positive, _ = condition
    negative = remote.node('ConditioningZeroOut', conditioning=positive)
    model = remote.node('UNETLoader', unet_name=h3.MODEL, weight_dtype='default')
    vae = remote.node('VAELoader', vae_name=h3.VAE)
    # The multi-output tuple is awaitable; a single output awaits to one ready ref.
    positive_ref, latent_ref = await condition
    negative_ref, model_ref, vae_ref = await asyncio.gather(negative, model, vae)
    return Setup(model_ref, positive_ref, latent_ref, negative_ref, vae_ref)


async def generate(remote, reused, seed, index, origin, steps):
    """One independent eager image. No graph capture or implicit result cache."""
    started = time.monotonic()
    # Retain Calls here because the benchmark records their invocation IDs.
    sampled = remote.node('KSampler', model=reused.model, positive=reused.positive,
                          negative=reused.negative, latent_image=reused.latent,
                          seed=seed, steps=steps, cfg=1.0, sampler_name='euler',
                          scheduler='simple', denoise=1.0, return_call=True)
    passed = remote.python(code='def identity(value):\n    return value\n', entry='identity',
                           inputs={'value': sampled.output()}, return_call=True)
    decoded = remote.node('VAEDecode', samples=passed.output(), vae=reused.vae, return_call=True)
    output = decoded.output()
    enqueued = time.monotonic()
    try:
        image_ref = await output
    except BaseException:
        output.release()
        raise
    ready = time.monotonic()
    row = {'index': index, 'seed': seed,
           'call_ids': {'sample': sampled.id, 'python': passed.id, 'decode': decoded.id},
           'submitted_offset_seconds': started - origin,
           'ready_offset_seconds': ready - origin,
           'enqueue_seconds': enqueued - started,
           'await_output_seconds': ready - enqueued}
    return image_ref, row


def check_environment(info, args):
    environment = info["extra"].get("environment", {})
    actual = (
        (args.expected_python, environment.get("python_version"), "Python"),
        (args.expected_torch, environment.get("packages", {}).get("torch", {}).get("version"), "Torch"),
        (args.expected_comfy_commit, info["extra"].get("source", {}).get("commit"), "ComfyUI"),
    )
    for expected, installed, label in actual:
        if expected and installed != expected:
            raise ValueError(f"{label} is {installed}; expected {expected}")


def save_image(image, name, directory):
    """Benchmark-only materialization: exact float Tensor plus a diagnostic PNG."""
    import numpy as np
    import torch
    from PIL import Image

    image = image.detach().cpu()
    assert image.ndim == 4 and image.shape[0] == 1 and image.shape[-1] == 3
    assert torch.isfinite(image).all()
    png, floats = directory / (name + ".png"), directory / (name + ".pt")
    Image.fromarray(np.clip(image[0].numpy() * 255, 0, 255).astype(np.uint8)).save(png)
    torch.save(image, floats)
    return image, {"png": str(png.resolve()), "float_tensor": str(floats.resolve()),
                   "png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
                   "float_sha256": hashlib.sha256(floats.read_bytes()).hexdigest()}


def save(image, name, directory):
    # This whole operation runs in a thread, retaining the local tensor until done.
    image, files = save_image(image, name, directory)
    array = image.contiguous().numpy()
    return {**files, 'shape': list(image.shape), 'dtype': str(image.dtype),
            'raw_tensor_sha256': hashlib.sha256(memoryview(array).cast('B')).hexdigest()}


async def finish_thread(function, *args):
    """Cancellation waits for a running disk write; it never reports it as success."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return result


def write_record(directory, record):
    temporary = directory / 'run.json.tmp'
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(directory / 'run.json')


async def materialize(ready, directory, record, origin):
    ref, row = ready
    started = time.monotonic()
    row['fetch_started_offset_seconds'] = started - origin
    try:
        image = await ref.fetch()
    finally:
        ref.release()
    fetched = time.monotonic()
    name = f"{row['index']:03d}-seed{row['seed']}"
    files = await finish_thread(save, image, name, directory)
    saved = time.monotonic()
    row.update(files, fetch_seconds=fetched - started, save_seconds=saved - fetched,
               fetched_offset_seconds=fetched - origin, saved_offset_seconds=saved - origin,
               latency_seconds=saved - origin - row['submitted_offset_seconds'])
    record['images'].append(row)
    await finish_thread(write_record, directory, record)
    print(json.dumps(row), flush=True)


async def run_images(remote, reused, seeds, directory, record, *, steps, serial=False):
    """Fixed bound: one current materialization plus at most one next image."""
    origin = time.monotonic()
    current = None
    next_task = None
    try:
        current = await generate(remote, reused, seeds[0], 0, origin, steps)
        for index in range(len(seeds)):
            if not serial and index + 1 < len(seeds):
                next_task = asyncio.create_task(
                    generate(remote, reused, seeds[index + 1], index + 1, origin, steps))
            await materialize(current, directory, record, origin)
            current = None
            if next_task is not None:
                current = await next_task
                next_task = None
            elif index + 1 < len(seeds):
                current = await generate(remote, reused, seeds[index + 1], index + 1, origin, steps)
    finally:
        if next_task is not None:
            next_task.cancel()
            result, = await asyncio.gather(next_task, return_exceptions=True)
            if isinstance(result, tuple):
                result[0].release()
        if current is not None:
            current[0].release()
    elapsed = time.monotonic() - origin
    record.update(generation_seconds=elapsed,
                  first_image_seconds_after_setup=record['images'][0]['saved_offset_seconds'],
                  images_per_minute_generation=len(seeds) * 60 / elapsed)


async def main(args):
    client_started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=True)
    package = Path(cf.__file__).parent
    record = {'status': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'h3_script_sha256': hashlib.sha256(Path(h3.__file__).read_bytes()).hexdigest(),
              'client_version': importlib.metadata.version('comfier'),
              'client_source_sha256': {
                  str(path.relative_to(package.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in package.rglob('*.py')},
              'mode': 'serial' if args.serial else 'overlap',
              'max_images_in_flight': 1 if args.serial else 2,
              'seeds': args.seeds, 'prompt': args.prompt,
              'model': h3.MODEL, 'model_sha256': h3.MODEL_SHA256,
              'width': args.width, 'height': args.height, 'steps': args.steps,
              'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'images': []}
    try:
        async with cf.Client(args.url, token=os.environ.get('COMFIER_TOKEN')) as remote:
            started = time.monotonic()
            acquired = await h3.prepare(args)
            record['remote'] = {'info': asdict(await remote.comfyui.info()),
                                'models': [asdict(file) for file in acquired]}
            check_environment(record['remote']['info'], args)
            record['preparation_seconds'] = time.monotonic() - started
            started = time.monotonic()
            reused = await setup(remote, args)
            record['setup_seconds'] = time.monotonic() - started
            generation_started = time.monotonic()
            try:
                await run_images(remote, reused, args.seeds, args.output, record,
                                 steps=args.steps, serial=args.serial)
            finally:
                reused.release()
            elapsed = record['setup_seconds'] + record['generation_seconds']
            record.update(first_image_seconds_including_setup=record['setup_seconds']
                          + record['first_image_seconds_after_setup'],
                          first_image_seconds_from_client_start=generation_started - client_started
                          + record['first_image_seconds_after_setup'],
                          images_per_minute_including_setup=len(args.seeds) * 60 / elapsed)
        record['status'] = 'complete'
    except BaseException as error:
        record['status'] = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
        record['error_type'] = type(error).__name__
        raise
    finally:
        record['client_total_seconds'] = time.monotonic() - client_started
        if record['status'] == 'complete':
            record['images_per_minute_client_total'] = len(args.seeds) * 60 / record['client_total_seconds']
        record['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
        await finish_thread(write_record, args.output, record)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    h3.add_arguments(parser, benchmark=True)
    parser.add_argument('--expected-python', type=str, help='optional exact target Python assertion')
    parser.add_argument('--expected-torch', type=str, help='optional exact Torch version assertion')
    parser.add_argument('--expected-comfy-commit', type=str, help='optional exact installed core assertion')
    parser.add_argument('--seeds', type=int, nargs='+', default=[426390, 426391, 426392])
    parser.add_argument('--serial', action='store_true', help='disable compute/fetch overlap')
    args = parser.parse_args()
    if len(set(args.seeds)) != len(args.seeds) or any(not 0 <= seed < 2**64 for seed in args.seeds):
        parser.error('seeds must be distinct integers in [0, 2**64)')
    asyncio.run(main(args))
