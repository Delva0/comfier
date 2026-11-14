"""Run independent CUDA diagnostics and collect evidence through real Comani."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from comani import Client


def collect():
    import json
    import os
    import resource
    import shutil
    import sys
    from pathlib import Path

    import comfy.model_management as mm
    import psutil
    import torch
    from comfy.cli_args import args

    root = Path('/workspace/comani-acceptance')
    result = {}
    for name in ['hardware.json', 'boot.json', 'model-verification.json',
                 'resource-peaks.json', 'configured-daemon.json']:
        path = root / name
        if path.exists():
            result[name] = json.loads(path.read_text())
    for name in ['daemon.log', 'probe.log', 'bootstrap.log', 'model-download.log',
                 'resource-samples.jsonl']:
        path = root / name
        if path.exists():
            result[name] = path.read_text(errors='replace')
    result['cgroup'] = {}
    for name in ['/proc/self/cgroup', '/sys/fs/cgroup/memory.max',
                 '/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory/memory.limit_in_bytes',
                 '/sys/fs/cgroup/memory/memory.max_usage_in_bytes',
                 '/sys/fs/cgroup/memory/memory.usage_in_bytes']:
        path = Path(name)
        if path.is_file():
            result['cgroup'][name] = path.read_text()
    result['runtime-system.json'] = {
        'pid': os.getpid(), 'argv': sys.argv, 'native_reserve_vram_gib': args.reserve_vram,
        'native_vram_state': str(mm.vram_state), 'minimum_inference_memory_bytes': mm.minimum_inference_memory(),
        'process_rss_bytes': psutil.Process().memory_info().rss,
        'kernel_process_rss_highwater_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        'disk_bytes': dict(zip(['total', 'used', 'free'], shutil.disk_usage(root))),
        'torch_max_allocated_bytes': torch.cuda.max_memory_allocated(),
        'torch_max_reserved_bytes': torch.cuda.max_memory_reserved(),
        'loaded_models_snapshot': [
            {'class': loaded.model.model.__class__.__name__, 'load_device': str(loaded.model.load_device),
             'offload_device': str(loaded.model.offload_device), 'total_model_bytes': loaded.model_memory(),
             'loaded_model_bytes': loaded.model_loaded_memory(), 'dynamic': loaded.model.is_dynamic()}
            for loaded in mm.current_loaded_models],
    }
    return result


def progress():
    import json
    import time
    from pathlib import Path

    root = Path('/workspace/comani-acceptance')
    manifest = json.loads((root / 'next/scripts/remote/models.json').read_text())
    result = {'unix_time': time.time(), 'models': []}
    for name, model in manifest.items():
        path = root / 'ComfyUI/models' / model['filename']
        partial = path.with_suffix(path.suffix + '.part')
        actual = path if path.exists() else partial
        result['models'].append({'name': name, 'bytes': actual.stat().st_size if actual.exists() else 0,
                                 'expected': model['bytes'], 'final_file': path.exists(),
                                 'modified_unix_time': actual.stat().st_mtime if actual.exists() else None})
    pid = int((root / 'download.pid').read_text())
    result['download_process_alive'] = (Path('/proc') / str(pid) / 'cmdline').exists()
    result['download_proc_io'] = (Path('/proc') / str(pid) / 'io').read_text()
    return result


def retry_download():
    """Validate ordinary HTTP resume, then stop only this task's sole writer."""
    import json
    import os
    import signal
    import time
    from pathlib import Path
    from urllib.request import Request, urlopen

    root = Path('/workspace/comani-acceptance')
    model = json.loads((root / 'next/scripts/remote/models.json').read_text())['diffusion']
    partial = (root / 'ComfyUI/models' / model['filename']).with_suffix('.safetensors.part')
    offset = partial.stat().st_size
    with urlopen(Request(model['url'], headers={'Range': f'bytes={offset}-{offset + 1023}'}), timeout=30) as response:
        status, content_range = response.status, response.headers.get('Content-Range')
        assert status == 206 and content_range.startswith(f'bytes {offset}-'), (status, content_range)
        assert content_range.endswith('/' + str(model['bytes']))
    pid = int((root / 'download.pid').read_text())
    proc = Path('/proc') / str(pid)
    assert b'next/scripts/remote/download_models.py' in (proc / 'cmdline').read_bytes()
    os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        if not proc.exists() or (proc / 'stat').read_text().split()[2] == 'Z':
            return {'writer_stopped': True, 'previous_pid': pid, 'range_status': status,
                    'content_range': content_range, 'partial_bytes': partial.stat().st_size}
        time.sleep(0.1)
    raise RuntimeError('Original download writer did not terminate; do not start another')


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    scripts = Path(__file__).parent
    async with Client(os.environ['COMANI_URL'], token=os.environ['COMANI_TOKEN']) as remote:
        if args.mode == 'diagnostics':
            result = await (await remote.python(source=scripts / 'nvfp4_diagnostics.py', entry='run').output()).fetch()
            (args.output / 'nvfp4-diagnostics.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)
            result = await (await remote.python(source=scripts / 'resource_monitor.py', entry='start').output()).fetch()
            print(json.dumps(result), flush=True)
        elif args.mode == 'configure':
            result = await (await remote.python(source=scripts / 'configured_daemon.py', entry='schedule').output()).fetch()
            (args.output / 'configured-daemon.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)
        elif args.mode in {'progress', 'retry-download'}:
            operation = progress if args.mode == 'progress' else retry_download
            result = await (await remote.python(operation).output()).fetch()
            (args.output / (args.mode + '.json')).write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)
        else:
            peaks = await (await remote.python(source=scripts / 'resource_monitor.py', entry='stop').output()).fetch()
            print(json.dumps(peaks), flush=True)
            evidence = await (await remote.python(collect).output()).fetch()
            for name, value in evidence.items():
                path = args.output / (name if '.' in name else name + '.json')
                path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + '\n')
            print(json.dumps({'collected': list(evidence)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['diagnostics', 'collect', 'progress', 'retry-download', 'configure'])
    parser.add_argument('--output', required=True, type=Path)
    asyncio.run(main(parser.parse_args()))
