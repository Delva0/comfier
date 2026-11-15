"""Reuse H3 setup refs, with at most one generation and one fetch/save in flight.

Set COMANI_URL and COMANI_TOKEN. The authenticated daemon must already have the
verified model files used by minimax_h3_source_remote.py. Nothing is installed.
Use --serial for the same eager calls without compute/fetch overlap.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import minimax_h3_remote as h3

import comani
from comani import Client, RemoteRef

OPS = Path(__file__).with_name('minimax_h3_source_ops.py')


@dataclass
class Setup:
    model: RemoteRef
    positive: RemoteRef
    latent: RemoteRef
    negative: RemoteRef
    vae: RemoteRef

    def release(self):
        for ref in (self.model, self.positive, self.latent, self.negative, self.vae):
            ref.release()


async def setup(remote):
    """Keep unchanged inputs alive; native ComfyUI still controls VRAM/offload."""
    clip = remote.node('CLIPLoader', clip_name=h3.ENCODER, type='minimax', device='default')
    condition = remote.node('MiniMaxH3ImageConditioning', clip=clip.output(),
                            prompt=h3.PROMPT, width=h3.WIDTH, height=h3.HEIGHT)
    negative = remote.node('ConditioningZeroOut', conditioning=condition.output(0))
    model = remote.node('UNETLoader', unet_name=h3.MODEL, weight_dtype='default')
    vae = remote.node('VAELoader', vae_name=h3.VAE)
    # A Call awaits to a tuple of ready refs. These refs themselves are not awaitable.
    positive_ref, latent_ref = await condition
    (negative_ref,), (model_ref,), (vae_ref,) = await asyncio.gather(negative, model, vae)
    return Setup(model_ref, positive_ref, latent_ref, negative_ref, vae_ref)


async def generate(remote, reused, seed, index, origin):
    """One independent eager image. No graph capture or implicit result cache."""
    started = time.monotonic()
    sampled = remote.node('KSampler', model=reused.model, positive=reused.positive,
                          negative=reused.negative, latent_image=reused.latent,
                          seed=seed, steps=h3.STEPS, cfg=1.0, sampler_name='euler',
                          scheduler='simple', denoise=1.0)
    passed = remote.python(source=OPS, entry='identity', inputs={'value': sampled.output()})
    decoded = remote.node('VAEDecode', samples=passed.output(), vae=reused.vae)
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


def save(image, name, directory):
    # This whole operation runs in a thread, retaining the local tensor until done.
    image, files = h3.save_image(image, name, directory)
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


async def run_images(remote, reused, seeds, directory, record, *, serial=False):
    """Fixed bound: one current materialization plus at most one next image."""
    origin = time.monotonic()
    current = None
    next_task = None
    try:
        current = await generate(remote, reused, seeds[0], 0, origin)
        for index in range(len(seeds)):
            if not serial and index + 1 < len(seeds):
                next_task = asyncio.create_task(
                    generate(remote, reused, seeds[index + 1], index + 1, origin))
            await materialize(current, directory, record, origin)
            current = None
            if next_task is not None:
                current = await next_task
                next_task = None
            elif index + 1 < len(seeds):
                current = await generate(remote, reused, seeds[index + 1], index + 1, origin)
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
    package = Path(comani.__file__).parent
    record = {'status': 'running', 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_ops_sha256': hashlib.sha256(OPS.read_bytes()).hexdigest(),
              'client_version': importlib.metadata.version('comani'),
              'client_source_sha256': {
                  str(path.relative_to(package.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in package.rglob('*.py')},
              'mode': 'serial' if args.serial else 'overlap',
              'max_images_in_flight': 1 if args.serial else 2,
              'seeds': args.seeds, 'prompt': h3.PROMPT,
              'model': h3.MODEL, 'model_sha256': h3.MODEL_SHA256,
              'width': h3.WIDTH, 'height': h3.HEIGHT, 'steps': h3.STEPS,
              'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'images': []}
    try:
        async with Client(os.environ['COMANI_URL'], token=os.environ.get('COMANI_TOKEN')) as remote:
            started = time.monotonic()
            call = remote.python(source=OPS, entry='provenance',
                                 inputs={'verification_path': args.verification_path})
            provenance_ref = await call.output()
            try:
                record['remote'] = await provenance_ref.fetch()
            finally:
                provenance_ref.release()
            record['provenance_seconds'] = time.monotonic() - started
            models = record['remote']['model_verification']['models']
            if not all(model['verified'] for model in models) or not any(
                    model['sha256'] == h3.MODEL_SHA256 for model in models):
                raise RuntimeError('The required H3 model verification did not pass')
            started = time.monotonic()
            reused = await setup(remote)
            record['setup_seconds'] = time.monotonic() - started
            generation_started = time.monotonic()
            try:
                await run_images(remote, reused, args.seeds, args.output, record, serial=args.serial)
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
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seeds', type=int, nargs='+', default=[426390, 426391, 426392])
    parser.add_argument('--serial', action='store_true', help='disable compute/fetch overlap')
    parser.add_argument('--verification-path',
                        default='/workspace/comani-acceptance/model-verification.json')
    args = parser.parse_args()
    if len(set(args.seeds)) != len(args.seeds) or any(not 0 <= seed < 2**64 for seed in args.seeds):
        parser.error('seeds must be distinct integers in [0, 2**64)')
    asyncio.run(main(args))
