"""Shared disk accounting for fresh downloads and normal retry/resume."""
import hashlib
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 << 20), b''):
            value.update(chunk)
    return value.hexdigest()


def remaining_model_bytes(manifest, model_root):
    remaining = 0
    for item in manifest.values():
        target = model_root / item['filename']
        partial = target.with_suffix(target.suffix + '.part')
        if target.exists():
            if target.stat().st_size != item['bytes'] or digest(target) != item['sha256']:
                raise RuntimeError(f'Existing model fails verification; retained for inspection: {target.name}')
            continue
        present = partial.stat().st_size if partial.exists() else 0
        if present > item['bytes']:
            raise RuntimeError(f'Partial file exceeds declared model size: {partial.name}')
        remaining += item['bytes'] - present
    return remaining


def environment_increment(project):
    # A ready environment has already consumed its disk allocation.
    return (512 << 20) if (project / '.local/runtime-ready.json').exists() else (10 << 30)


OUTPUT_RESERVE = 1 << 30
