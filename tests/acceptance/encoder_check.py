"""H3 encoder fixture against an existing daemon and already provisioned weights."""
import argparse
import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path

from comani import Client


def describe(conditioning, latent):
    import torch
    return {'conditioning_shape': list(conditioning[0][0].shape),
            'conditioning_finite': bool(torch.isfinite(conditioning[0][0]).all()),
            'latent_shape': list(latent['samples'].shape)}


async def main(args):
    spec = importlib.util.spec_from_file_location('actual_h3_example', Path(__file__).parents[2] / 'examples/minimax_h3_remote.py')
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    started = time.monotonic()
    async with Client(args.url, token=os.environ.get('COMANI_TOKEN')) as remote:
        clip = remote.node('CLIPLoader', return_call=True, clip_name=example.ENCODER, type='minimax', device='default')
        call = remote.node('MiniMaxH3ImageConditioning', return_call=True, clip=clip.output(), prompt=example.PROMPT,
                           width=example.WIDTH, height=example.HEIGHT)
        details = await (await remote.python(describe, inputs={
            'conditioning': call.output(0), 'latent': call.output(1)}).output()).fetch()
        assert details['conditioning_finite']
        result = {'passed': True, 'seconds': time.monotonic() - started, 'clip_call_id': clip.id,
                  'conditioning_call_id': call.id, **details}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--url', default=os.environ.get('COMANI_URL'))
    args = parser.parse_args()
    if not args.url:
        parser.error('Provide --url or COMANI_URL')
    asyncio.run(main(args))
