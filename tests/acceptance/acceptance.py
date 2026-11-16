"""Independent local SDK -> real remote daemon acceptance, with optional H3 run.

This never starts a local daemon or substitutes local model weights. Results
are written only after real operations finish. Use a private SSH tunnel URL or
provide COMANI_TOKEN for an authenticated HTTPS endpoint. Run only against a
disposable test daemon: one case deliberately evicts its native cache. The client
requires its normal tensor/serializer dependencies; this script installs nothing.
"""
import argparse
import asyncio
import gc
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from comani import Client, Expired, PackageConflict, Released, RemoteError, SessionClosed


def delayed(value, seconds):
    import time
    time.sleep(seconds)
    return value


def fail():
    raise ValueError('independent remote acceptance sentinel')


def metadata():
    import hashlib
    import importlib.metadata
    import platform
    import socket
    import subprocess
    from pathlib import Path

    import comfy.model_management as mm
    import torch
    from comfy.cli_args import args

    import comani

    package = Path(comani.__file__).parent
    comfy_root = Path(mm.__file__).resolve().parents[1]
    try:
        commit = subprocess.check_output(['git', '-C', str(comfy_root), 'rev-parse', 'HEAD'], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    device = mm.get_torch_device()
    device_name = torch.cuda.get_device_name(device) if device.type == 'cuda' else str(device)
    return {'host': socket.gethostname(), 'python': platform.python_version(),
            'cloudpickle': importlib.metadata.version('cloudpickle'),
            'device': device_name, 'torch': torch.__version__,
            'comani_file': comani.__file__, 'comani_version': importlib.metadata.version('comani'),
            'installed_source_sha256': {str(path.relative_to(package.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in package.rglob('*.py')},
            'native_reserve_vram_gib': args.reserve_vram,
            'native_vram_state': str(mm.vram_state),
            'minimum_inference_memory_bytes': mm.minimum_inference_memory(),
            'comfy_root': str(comfy_root), 'comfy_commit': commit}


def explicit_test_cache_eviction():
    """Native cache control inside this disposable daemon, solely for testing."""
    import gc

    import execution
    executors = [obj for obj in gc.get_objects() if isinstance(obj, execution.PromptExecutor)]
    if len(executors) != 1:
        raise RuntimeError('Expected exactly this daemon native executor')
    executors[0].caches.outputs.cache.clear()
    return len(executors)


def model_prompt(prompt, width, height, seed):
    return {
        'model': {'class_type': 'UNETLoader', 'inputs': {
            'unet_name': 'minimax_H3_image_pruned_INT8.safetensors', 'weight_dtype': 'default'}},
        'clip': {'class_type': 'CLIPLoader', 'inputs': {
            'clip_name': 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'type': 'minimax', 'device': 'default'}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': 'minimax_h3_image_vae_fp16.safetensors'}},
        'condition': {'class_type': 'MiniMaxH3ImageConditioning', 'inputs': {
            'clip': ['clip', 0], 'prompt': prompt, 'width': width, 'height': height}},
        'negative': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['condition', 0]}},
        'sample': {'class_type': 'KSampler', 'inputs': {
            'model': ['model', 0], 'seed': seed, 'steps': 6, 'cfg': 1.0,
            'sampler_name': 'euler', 'scheduler': 'simple', 'positive': ['condition', 0],
            'negative': ['negative', 0], 'latent_image': ['condition', 1], 'denoise': 1.0}},
        'decode': {'class_type': 'VAEDecode', 'inputs': {'samples': ['sample', 0], 'vae': ['vae', 0]}},
    }


async def main(args):
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'cases': []}
    args.output.mkdir(parents=True, exist_ok=True)

    def record(name, **details):
        report['cases'].append({'name': name, 'passed': True, **details})
        (args.output / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report['cases'][-1]), flush=True)

    token = os.environ.get('COMANI_TOKEN')
    remote = Client(args.url, token=token)
    try:
        await remote.start()
        assert remote.handshake.get('capabilities', {}).get('bounded_wait') is True
        info = await (await remote.python(metadata).output()).fetch()
        import hashlib
        import zipfile
        wheel_sha256 = hashlib.sha256(args.wheel.read_bytes()).hexdigest()
        if args.wheel_sha256:
            assert wheel_sha256 == args.wheel_sha256
        with zipfile.ZipFile(args.wheel) as wheel:
            expected_sources = {name: hashlib.sha256(wheel.read(name)).hexdigest()
                                for name in wheel.namelist() if name.startswith('comani/') and name.endswith('.py')}
        assert info['installed_source_sha256'] == expected_sources
        if args.expected_device:
            assert args.expected_device in info['device']
        if args.expected_comfy_commit:
            assert info['comfy_commit'] == args.expected_comfy_commit
        if args.expected_reserve_vram is not None:
            assert info['native_reserve_vram_gib'] == args.expected_reserve_vram
        record('runtime_metadata_and_installed_source_identity', client_wheel_sha256=wheel_sha256, **info)
        started = time.monotonic()
        first = remote.node('EmptyImage', return_call=True, width=64, height=64, batch_size=1, color=0x204080)
        transformed = remote.python(lambda x: x * 0.5, inputs={'x': first.output()})
        last = remote.node('ImageInvert', return_call=True, image=transformed.output())
        submitted_seconds = time.monotonic() - started
        ref = await last.output()
        tensor = await ref.fetch()
        import torch
        expected = 1 - torch.tensor([0x20, 0x40, 0x80], dtype=torch.float32) / 255 * 0.5
        torch.testing.assert_close(tensor[0, 0, 0], expected)
        assert ref.ownership == 'owned'
        record('eager_node_python_node_pending_refs', shape=list(tensor.shape), submission_seconds=submitted_seconds)

        slow = remote.python(delayed, args=(17, 2.0))
        await slow._state.accepted
        started = time.monotonic()
        await remote.transport.request('heartbeat', session_id=remote.session_id)
        responsive = time.monotonic() - started
        assert responsive < 1.5
        assert await (await slow.output()).fetch() == 17
        record('driver_responds_during_sync_compute', heartbeat_seconds=responsive)

        with remote.graph() as graph:
            entry = graph.input('image', type='IMAGE')
            middle, = await remote.node('ImageInvert', return_call=True, image=entry)
            final = remote.python(delayed, args=(middle, 2.0))
            graph.output('image', final.output())
        run = remote.submit(graph, inputs={'image': first.output()})
        keep = remote.python(lambda value: value, inputs={'value': run.output('image')})
        await keep._state.accepted
        intermediate = await run[middle]
        assert intermediate.ownership == 'borrowed'
        assert run.status != 'completed'
        borrowed = await run.output('image')
        owned = await keep.output()
        assert owned.ownership == 'owned'
        before = await owned.fetch()
        record('native_capture_and_early_intermediate', intermediate_ownership=intermediate.ownership)
        await remote.python(explicit_test_cache_eviction)
        try:
            await borrowed.fetch()
        except Expired:
            pass
        else:
            raise AssertionError('Borrowed workflow output survived explicit native cache eviction')
        torch.testing.assert_close(await owned.fetch(), before)
        record('borrowed_expired_and_explicit_eager_consumer_retained')

        producer = remote.python(lambda: {'items': [2, 3]})
        pending = producer.output()
        consumer = remote.python(delayed, args=([pending, pending], 0.2))
        await consumer._state.accepted
        del producer, pending
        gc.collect()
        assert await (await consumer.output()).fetch() == [{'items': [2, 3]}, {'items': [2, 3]}]
        record('accepted_consumer_survives_local_gc')
        alias = owned
        owned.release()
        try:
            await alias.fetch()
        except Released:
            pass
        else:
            raise AssertionError('Released aliases remained usable')
        record('release_invalidates_aliases')

        bad = remote.python(fail)
        dependent = remote.python(lambda value: value, inputs={'value': bad.output()})
        for call in (bad, dependent):
            try:
                await call
            except RemoteError:
                pass
            else:
                raise AssertionError('Producer/consumer failure was not reported')
        assert await (await remote.python(lambda: 42).output()).fetch() == 42
        record('producer_error_dependency_error_and_worker_recovery')

        with tempfile.TemporaryDirectory(prefix='comani-remote-acceptance-') as temporary:
            directory = Path(temporary)
            source = directory / 'operation.py'
            source.write_text('def apply(value):\n    return value * 3\n')
            assert await (await remote.python(source=source, entry='apply', inputs={'value': 7}).output()).fetch() == 21
            package = directory / ('acceptance_package_' + str(time.time_ns()))
            package.mkdir()
            init = package / '__init__.py'
            init.write_text('counter = 0\ndef apply():\n    global counter\n    counter += 1\n    return counter\n')
            entry = package.name + ':apply'
            assert await (await remote.python(source=package, entry=entry).output()).fetch() == 1
            assert await (await remote.python(source=package, entry=entry).output()).fetch() == 2
            init.write_text('def apply():\n    return 999\n')
            try:
                await remote.python(source=package, entry=entry)
            except PackageConflict:
                pass
            else:
                raise AssertionError('Changed same-name package was silently replaced')
        record('source_file_package_reuse_and_conflict')

        if args.model:
            await test_model(remote, args, record)
        dangling = await remote.python(lambda: 7).output()
        await remote.close()
        try:
            await dangling.fetch()
        except SessionClosed:
            pass
        else:
            raise AssertionError('Closed session reference remained usable')
        record('session_close_invalidates_refs')
        report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        report['passed'] = True
    except Exception as error:
        report['passed'] = False
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
        raise
    finally:
        (args.output / 'acceptance.json').write_text(json.dumps(report, indent=2) + '\n')
        if not remote.closed:
            await remote.close(wait=False)


async def test_model(remote, args, record):
    import numpy as np
    import torch
    from PIL import Image
    prompt = ('A detailed studio photograph of a small ceramic red fox sitting on a dark blue '
              'wooden table. The fox has white ear tips and a white chest, delicate painted '
              'fur grooves, and a soft satin glaze. Warm sunlight enters from the upper left. '
              'A small green plant and a closed cream sketchbook sit behind it. The fox is '
              'centered, fully visible, with crisp detail, believable shadows, and no lettering.')
    raw = model_prompt(prompt, 2048, 2048, 426190)
    (args.output / 'native_prompt.json').write_text(json.dumps(raw, indent=2) + '\n')

    async def eager():
        clip = remote.node('CLIPLoader', return_call=True, **raw['clip']['inputs'])
        condition = remote.node('MiniMaxH3ImageConditioning', return_call=True, clip=clip.output(),
                                prompt=prompt, width=2048, height=2048)
        negative = remote.node('ConditioningZeroOut', return_call=True, conditioning=condition.output(0))
        model = remote.node('UNETLoader', return_call=True, **raw['model']['inputs'])
        sampled = remote.node('KSampler', return_call=True, model=model.output(), seed=426190, steps=6, cfg=1.0,
                              sampler_name='euler', scheduler='simple', positive=condition.output(0),
                              negative=negative.output(), latent_image=condition.output(1), denoise=1.0)
        # This Python call is a real in-memory latent dependency in the model path.
        passed = remote.python(lambda latent: latent, inputs={'latent': sampled.output()})
        vae = remote.node('VAELoader', return_call=True, **raw['vae']['inputs'])
        decoded = remote.node('VAEDecode', return_call=True, samples=passed.output(), vae=vae.output())
        return await (await decoded.output()).fetch()

    started = time.monotonic()
    image = await eager()
    assert tuple(image.shape) == (1, 2048, 2048, 3) and torch.isfinite(image).all()
    image = image.cpu()
    Image.fromarray(np.clip(image[0].numpy() * 255, 0, 255).astype(np.uint8)).save(args.output / 'eager.png')
    record('requested_int8_h3_eager_full_generation', seconds=time.monotonic() - started,
           shape=list(image.shape), steps=6, seed=426190, resolution='2048x2048')
    gc.collect()
    started = time.monotonic()
    run = remote.submit(raw, outputs={'image': ['decode', 0]}, targets=['decode'])
    kept = remote.python(lambda image: image, inputs={'image': run.output('image')})
    await kept._state.accepted
    reference = (await (await kept.output()).fetch()).cpu()
    error = float((image - reference).abs().max())
    torch.testing.assert_close(image, reference, rtol=1e-4, atol=1e-4)
    Image.fromarray(np.clip(reference[0].numpy() * 255, 0, 255).astype(np.uint8)).save(args.output / 'native_graph.png')
    record('requested_int8_h3_native_api_graph_matches_eager', seconds=time.monotonic() - started,
           max_abs_error=error, shape=list(reference.shape))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model', action='store_true', help='Optional H3 fixture; requires pre-existing requested weights')
    parser.add_argument('--wheel', required=True, type=Path, help='Client-side wheel whose Python files must match the remote installation')
    parser.add_argument('--wheel-sha256')
    parser.add_argument('--expected-device')
    parser.add_argument('--expected-comfy-commit')
    parser.add_argument('--expected-reserve-vram', type=float)
    asyncio.run(main(parser.parse_args()))
