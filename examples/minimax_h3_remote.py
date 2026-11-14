"""Generate the acceptance PNGs using the actual Comani package and H3 INT8.

Set COMANI_URL and COMANI_TOKEN. The token is never stored in this script.
Both modes use the same fixed prompt, seed, full 2048x2048 canvas, and official
distilled six-step Euler/simple schedule. Float tensors are saved beside PNGs.
"""
import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from comani import Client

MODEL = 'minimax_H3_image_pruned_INT8.safetensors'
MODEL_SHA256 = '4647bdf553a32aa4ae55d9eb28322be42d32611d333c19a097d2845ae8a9b7a8'
ENCODER = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
VAE = 'minimax_h3_image_vae_fp16.safetensors'
PROMPT = (
    'A detailed studio photograph of a small ceramic red fox sitting on a dark blue '
    'wooden table. The fox has white ear tips and a white chest, delicate painted '
    'fur grooves, and a soft satin glaze. Warm sunlight enters from the upper left. '
    'A small green plant and a closed cream sketchbook sit behind it. The fox is '
    'centered, fully visible, with crisp detail, believable shadows, and no lettering.'
)
SEED, WIDTH, HEIGHT, STEPS = 426190, 2048, 2048, 6


def pipeline(remote):
    clip = remote.node('CLIPLoader', clip_name=ENCODER, type='minimax', device='default')
    condition = remote.node('MiniMaxH3ImageConditioning', clip=clip.output(),
                            prompt=PROMPT, width=WIDTH, height=HEIGHT)
    negative = remote.node('ConditioningZeroOut', conditioning=condition.output(0))
    model = remote.node('UNETLoader', unet_name=MODEL, weight_dtype='default')
    sampled = remote.node('KSampler', model=model.output(), positive=condition.output(0),
                          negative=negative.output(), latent_image=condition.output(1),
                          seed=SEED, steps=STEPS, cfg=1.0, sampler_name='euler',
                          scheduler='simple', denoise=1.0)
    # Real Python processing of the in-memory latent between native nodes.
    passed = remote.python(lambda latent: latent, inputs={'latent': sampled.output()})
    vae = remote.node('VAELoader', vae_name=VAE)
    decoded = remote.node('VAEDecode', samples=passed.output(), vae=vae.output())
    return decoded.output(), decoded.id


def native_prompt():
    """The same pipeline as unmodified Comfy API JSON, without the identity bridge."""
    return {
        'clip': {'class_type': 'CLIPLoader', 'inputs': {
            'clip_name': ENCODER, 'type': 'minimax', 'device': 'default'}},
        'condition': {'class_type': 'MiniMaxH3ImageConditioning', 'inputs': {
            'clip': ['clip', 0], 'prompt': PROMPT, 'width': WIDTH, 'height': HEIGHT}},
        'negative': {'class_type': 'ConditioningZeroOut', 'inputs': {'conditioning': ['condition', 0]}},
        'model': {'class_type': 'UNETLoader', 'inputs': {'unet_name': MODEL, 'weight_dtype': 'default'}},
        'sample': {'class_type': 'KSampler', 'inputs': {
            'model': ['model', 0], 'positive': ['condition', 0], 'negative': ['negative', 0],
            'latent_image': ['condition', 1], 'seed': SEED, 'steps': STEPS, 'cfg': 1.0,
            'sampler_name': 'euler', 'scheduler': 'simple', 'denoise': 1.0}},
        'vae': {'class_type': 'VAELoader', 'inputs': {'vae_name': VAE}},
        'decode': {'class_type': 'VAEDecode', 'inputs': {'samples': ['sample', 0], 'vae': ['vae', 0]}},
    }


def remote_provenance():
    import importlib.metadata
    import json
    import platform
    import socket
    from pathlib import Path

    import comfy.model_management as mm
    import torch
    from comfy.cli_args import args

    import comani

    verification = json.loads(Path('/workspace/comani-acceptance/model-verification.json').read_text())
    return {'host': socket.gethostname(), 'python': platform.python_version(),
            'device': torch.cuda.get_device_name(0), 'torch': torch.__version__,
            'comani_file': comani.__file__, 'comani_version': importlib.metadata.version('comani'),
            'native_reserve_vram_gib': args.reserve_vram,
            'native_vram_state': str(mm.vram_state),
            'minimum_inference_memory_bytes': mm.minimum_inference_memory(),
            'model_verification': verification}


def save_image(image, name, directory):
    image = image.detach().cpu()
    assert tuple(image.shape) == (1, HEIGHT, WIDTH, 3) and torch.isfinite(image).all()
    png, floats = directory / (name + '.png'), directory / (name + '.pt')
    Image.fromarray(np.clip(image[0].numpy() * 255, 0, 255).astype(np.uint8)).save(png)
    torch.save(image, floats)
    return image, {'png': str(png.resolve()), 'float_tensor': str(floats.resolve()),
                   'png_sha256': hashlib.sha256(png.read_bytes()).hexdigest(),
                   'float_sha256': hashlib.sha256(floats.read_bytes()).hexdigest()}


async def main(args):
    if not args.url:
        raise SystemExit('Set COMANI_URL or pass --url')
    args.output.mkdir(parents=True, exist_ok=True)
    record = {'script': str(Path(__file__).resolve()), 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'prompt': PROMPT, 'seed': SEED, 'width': WIDTH, 'height': HEIGHT, 'steps': STEPS,
              'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple', 'model': MODEL,
              'model_sha256': MODEL_SHA256, 'wheel_sha256': args.wheel_sha256, 'runs': []}
    async with Client(args.url, token=os.environ.get('COMANI_TOKEN')) as remote:
        provenance = await (await remote.python(remote_provenance).output()).fetch()
        assert any(m['sha256'] == MODEL_SHA256 and m['verified']
                   for m in provenance['model_verification']['models'])
        assert '4090' in provenance['device']
        record['remote'] = provenance
        started = time.monotonic()
        pending, call_id = pipeline(remote)
        eager, files = save_image(await (await pending).fetch(), 'eager', args.output)
        record['runs'].append({'mode': 'eager_node_python_node', 'call_id': call_id,
                               'seconds': time.monotonic() - started, **files})
        (args.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps(record['runs'][-1]), flush=True)
        started = time.monotonic()
        with remote.graph() as graph:
            pending, node_id = pipeline(remote)
            graph.output('image', pending)
        run = remote.submit(graph)
        # This explicit eager consumer retains the graph's borrowed image.
        kept = remote.python(lambda image: image, inputs={'image': run.output('image')})
        native, files = save_image(await (await kept.output()).fetch(), 'captured_graph', args.output)
        error = float((eager - native).abs().max())
        torch.testing.assert_close(eager, native, rtol=1e-4, atol=1e-4)
        record['runs'].append({'mode': 'captured_native_graph_then_eager_retention',
                               'call_id': run.id, 'decode_node_id': node_id,
                               'consumer_call_id': kept.id, 'seconds': time.monotonic() - started,
                               'max_abs_float_error_vs_eager': error, **files})
        (args.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps(record['runs'][-1]), flush=True)
        started = time.monotonic()
        prompt = native_prompt()
        (args.output / 'native_prompt.json').write_text(json.dumps(prompt, indent=2) + '\n')
        run = remote.submit(prompt, outputs={'image': ['decode', 0]}, targets=['decode'])
        kept = remote.python(lambda image: image, inputs={'image': run.output('image')})
        reference, files = save_image(await (await kept.output()).fetch(), 'native_api', args.output)
        error = float((eager - reference).abs().max())
        torch.testing.assert_close(eager, reference, rtol=1e-4, atol=1e-4)
        record['runs'].append({'mode': 'native_api_json_reference', 'call_id': run.id,
                               'consumer_call_id': kept.id, 'seconds': time.monotonic() - started,
                               'max_abs_float_error_vs_eager': error, **files})
        record['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        record['passed'] = True
        (args.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
        print(json.dumps(record['runs'][-1]), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('COMANI_URL'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--wheel-sha256', required=True)
    asyncio.run(main(parser.parse_args()))
