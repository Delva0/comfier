"""Explicit source-file operations for the H3 remote example."""


def identity(value):
    return value


def provenance(verification_path):
    import hashlib
    import importlib.metadata
    import json
    import platform
    import subprocess
    from pathlib import Path

    import comfy.model_management as mm
    import torch
    from comfy.cli_args import args

    import comani

    package = Path(comani.__file__).parent
    return {
        'python': platform.python_version(),
        'device': torch.cuda.get_device_name(0),
        'torch': torch.__version__,
        'cloudpickle': importlib.metadata.version('cloudpickle'),
        'comani_file': comani.__file__,
        'comani_version': importlib.metadata.version('comani'),
        'installed_source_sha256': {
            str(path.relative_to(package.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob('*.py')
        },
        'comfy_commit': subprocess.check_output(
            ['git', '-C', str(Path(mm.__file__).resolve().parents[1]), 'rev-parse', 'HEAD'],
            text=True,
        ).strip(),
        'native_reserve_vram_gib': args.reserve_vram,
        'native_vram_state': str(mm.vram_state),
        'minimum_inference_memory_bytes': mm.minimum_inference_memory(),
        'model_verification': json.loads(Path(verification_path).read_text()),
    }
