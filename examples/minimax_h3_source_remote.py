"""Run the H3 example with explicit Python source files across Python versions.

Set COMANI_URL and COMANI_TOKEN. Requires the same three model files and local
Tensor dependencies as minimax_h3_remote.py; this script installs nothing.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import minimax_h3_remote as h3
import torch

from comani import Client

OPS = Path(__file__).with_name('minimax_h3_source_ops.py')


def pipeline(remote):
    clip = remote.node('CLIPLoader', clip_name=h3.ENCODER, type='minimax', device='default')
    condition = remote.node('MiniMaxH3ImageConditioning', clip=clip.output(),
                            prompt=h3.PROMPT, width=h3.WIDTH, height=h3.HEIGHT)
    negative = remote.node('ConditioningZeroOut', conditioning=condition.output(0))
    model = remote.node('UNETLoader', unet_name=h3.MODEL, weight_dtype='default')
    sampled = remote.node('KSampler', model=model.output(), positive=condition.output(0),
                          negative=negative.output(), latent_image=condition.output(1),
                          seed=h3.SEED, steps=h3.STEPS, cfg=1.0, sampler_name='euler',
                          scheduler='simple', denoise=1.0)
    passed = remote.python(source=OPS, entry='identity', inputs={'value': sampled.output()})
    vae = remote.node('VAELoader', vae_name=h3.VAE)
    return remote.node('VAEDecode', samples=passed.output(), vae=vae.output()).output()


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    record = {'script': str(Path(__file__).resolve()), 'source_ops': str(OPS.resolve()),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'source_ops_sha256': hashlib.sha256(OPS.read_bytes()).hexdigest(),
              'package_commit': args.package_commit, 'wheel_sha256': args.wheel_sha256,
              'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'prompt': h3.PROMPT, 'seed': h3.SEED, 'width': h3.WIDTH, 'height': h3.HEIGHT,
              'steps': h3.STEPS, 'cfg': 1.0, 'sampler': 'euler', 'scheduler': 'simple',
              'model': h3.MODEL, 'model_sha256': h3.MODEL_SHA256, 'runs': []}
    async with Client(os.environ['COMANI_URL'], token=os.environ.get('COMANI_TOKEN')) as remote:
        call = remote.python(source=OPS, entry='provenance',
                             inputs={'verification_path': args.verification_path})
        record['remote'] = await (await call.output()).fetch()
        assert '4090' in record['remote']['device']
        assert all(item['verified'] for item in record['remote']['model_verification']['models'])
        if args.monitor_source:
            await remote.python(source=args.monitor_source, entry='start',
                                inputs={'directory': '/workspace/comani-acceptance/resources'}).output()
        eager = None
        for mode in ('eager', 'captured_graph', 'native_api'):
            started = time.monotonic()
            if mode == 'eager':
                output = pipeline(remote)
                call_id = None
            else:
                if mode == 'captured_graph':
                    with remote.graph() as graph:
                        graph.output('image', pipeline(remote))
                    run = remote.submit(graph)
                else:
                    prompt = h3.native_prompt()
                    (args.output / 'native_prompt.json').write_text(json.dumps(prompt, indent=2) + '\n')
                    run = remote.submit(prompt, outputs={'image': ['decode', 0]}, targets=['decode'])
                kept = remote.python(source=OPS, entry='identity', inputs={'value': run.output('image')})
                output, call_id = kept.output(), run.id
            image, files = h3.save_image(await (await output).fetch(), mode, args.output)
            if eager is None:
                eager = image
            assert torch.equal(eager, image), mode + ' differs from eager'
            item = {'mode': mode, 'call_id': call_id, 'seconds': time.monotonic() - started,
                    'max_abs_float_error_vs_eager': float((eager - image).abs().max()), **files}
            record['runs'].append(item)
            (args.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')
            print(json.dumps(item), flush=True)
        if args.monitor_source:
            call = remote.python(source=args.monitor_source, entry='stop',
                                 inputs={'directory': '/workspace/comani-acceptance/resources'})
            record['resource_peaks'] = await (await call.output()).fetch()
        record['passed'] = True
        record['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
        (args.output / 'run.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--package-commit', required=True)
    parser.add_argument('--wheel-sha256', required=True)
    parser.add_argument('--verification-path', default='/workspace/comani-acceptance/model-verification.json')
    parser.add_argument('--monitor-source', type=Path)
    asyncio.run(main(parser.parse_args()))
