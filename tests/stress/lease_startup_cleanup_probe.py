"""Audit the two existing failed-open/cancelled-handshake HTTP reproductions.

Usage: python -I -B tests/stress/lease_startup_cleanup_probe.py MANIFEST AUDIT_JSON
       failed_open_probe --output RESULT_JSON
Use startup_cancel_probe for the other unchanged reproduction.
"""
import gc
import hashlib
import json
import os
import runpy
import sys
import threading
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
manifest_path, destination, probe_name, *probe_args = sys.argv[1:]
assert probe_name in {'failed_open_probe', 'startup_cancel_probe'}
destination = Path(destination)
destination.parent.mkdir(parents=True, exist_ok=True)


def provenance():
    import comfier
    package = Path(comfier.__file__).resolve().parent
    installed = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in package.rglob('*.py')}
    manifest = json.loads(Path(manifest_path).read_text())['files']
    expected = {name.removeprefix('src/comfier/'): value for name, value in manifest.items()
                if name.startswith('src/comfier/')}
    assert installed == expected and '/site-packages/comfier' in str(package)
    return {'package': str(package), 'installed_files': installed,
            'loaded_modules': {name: {'path': str(Path(module.__file__).resolve()),
                'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
                for name, module in list(sys.modules.items()) if name.startswith('comfier')
                and getattr(module, '__file__', '').endswith('.py')}}


def sockets():
    result = []
    for path in Path('/proc/self/fd').iterdir():
        try:
            target = os.readlink(path)
            if target.startswith('socket:'):
                result.append(target)
        except FileNotFoundError:
            pass
    return sorted(result)


before_threads = {thread.ident for thread in threading.enumerate()}
before_sockets = sockets()
probe = PROJECT / 'tests/stress' / (probe_name + '.py')
report = {'pid': os.getpid(), 'probe': str(probe), 'probe_args': probe_args,
          'probe_sha256': hashlib.sha256(probe.read_bytes()).hexdigest(),
          'before': provenance(), 'before_sockets': before_sockets}
sys.path.insert(0, str(probe.parent))
sys.argv = [str(probe), *probe_args]
try:
    runpy.run_path(str(probe), run_name='__main__')
    report['probe_exit_code'] = 0
except SystemExit as result:
    report['probe_exit_code'] = result.code or 0
except BaseException:
    report['probe_exit_code'] = 1
    report['probe_error'] = traceback.format_exc()
finally:
    for _ in range(50):
        gc.collect()
        extra = [thread for thread in threading.enumerate() if thread.ident not in before_threads]
        if not extra:
            break
        time.sleep(.02)
    report['extra_live_threads'] = [{'name': thread.name, 'ident': thread.ident, 'daemon': thread.daemon}
                                   for thread in threading.enumerate() if thread.ident not in before_threads]
    report['after_sockets'] = sockets()
    report['after'] = provenance()
    report['passed'] = (report['probe_exit_code'] == 0 and not report['extra_live_threads']
                        and before_sockets == report['after_sockets'])
    destination.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({key: report.get(key) for key in ('probe', 'probe_exit_code', 'extra_live_threads',
                 'before_sockets', 'after_sockets', 'passed')}), flush=True)
sys.exit(0 if report['passed'] else 1)
