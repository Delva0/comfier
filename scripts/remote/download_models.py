"""Download the requested model on the rented machine and verify exact SHA256.

No account tokens are needed for these public URLs. Authentication failures are
reported rather than bypassed. Existing correct files are reused, never copied
from a client's local model directory.
"""
import argparse
import hashlib
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from disk_budget import OUTPUT_RESERVE, environment_increment, remaining_model_bytes


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 << 20), b''):
            value.update(chunk)
    return value.hexdigest()


def download(name, item, root):
    target = root / item['filename']
    target.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if not (target.is_file() and target.stat().st_size == item['bytes']
            and digest(target) == item['sha256']):
        partial = target.with_suffix(target.suffix + '.part')
        for attempt in range(4):
            offset = partial.stat().st_size if partial.exists() else 0
            if offset == item['bytes']:
                break
            headers = {'User-Agent': 'comani-independent-remote-acceptance/1'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            try:
                with urlopen(Request(item['url'], headers=headers), timeout=60) as response:
                    if offset and response.status != 206:
                        offset = 0
                    mode = 'ab' if offset else 'wb'
                    written = offset
                    progress = written // (1 << 30)
                    with partial.open(mode) as output:
                        while chunk := response.read(4 << 20):
                            output.write(chunk)
                            written += len(chunk)
                            if written > item['bytes']:
                                raise RuntimeError(f'{name}: response exceeds expected file size')
                            if written // (1 << 30) > progress:
                                progress = written // (1 << 30)
                                print(json.dumps({'download': name, 'bytes': written, 'expected': item['bytes']}), flush=True)
                if partial.stat().st_size == item['bytes']:
                    break
            except HTTPError as error:
                if 400 <= error.code < 500:
                    raise RuntimeError(f'{name}: download HTTP {error.code}; authorization or source access must be resolved normally') from None
                if attempt == 3:
                    raise
                time.sleep(2)
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise
                time.sleep(2)
        if partial.stat().st_size != item['bytes']:
            raise RuntimeError(f'{name}: incomplete download')
        actual = digest(partial)
        if actual != item['sha256']:
            raise RuntimeError(f'{name}: SHA256 mismatch: {actual}')
        partial.replace(target)
    result = {'name': name, 'path': str(target), 'bytes': target.stat().st_size,
              'sha256': item['sha256'], 'verified': True,
              'elapsed_seconds': round(time.monotonic() - started, 3)}
    print(json.dumps(result), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('models.json').read_text())
    root = args.comfy_root.resolve() / 'models'
    root.mkdir(parents=True, exist_ok=True)
    import fcntl
    download_lock = (root / '.comani-download.lock').open('w')
    fcntl.flock(download_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    remaining = remaining_model_bytes(manifest, root)
    if shutil.disk_usage(root).free < remaining + environment_increment(Path(__file__).parents[2]) + OUTPUT_RESERVE:
        raise RuntimeError('Insufficient free disk for missing models, environment increment and output reserve')
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(download, name, item, root) for name, item in manifest.items()]
        for future in futures:
            results.append(future.result())
    report = {'verified_at_utc': datetime.now(timezone.utc).isoformat(), 'models': results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
