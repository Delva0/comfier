"""Explicit, auditable daemon restart via the authorized Comani Python channel.

The helper only restarts this task's verified daemon and executes the standard
project CLI with native Comfy arguments. It never edits installed libraries.
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path('/workspace/comani-acceptance')


def command(reserve_gib):
    return [sys.executable, '-B', '-m', 'comani._runtime',
            '--comfy-root', str(ROOT / 'ComfyUI'), '--host', '127.0.0.1',
            '--port', '8190', '--no-custom-nodes', '--', '--reserve-vram', str(reserve_gib)]


def schedule(reserve_gib=4.0):
    assert reserve_gib == 4.0
    old_pid = os.getpid()
    assert b'comani._runtime' in Path(f'/proc/{old_pid}/cmdline').read_bytes()
    record = {'old_pid': old_pid, 'command': command(reserve_gib),
              'wheel_sha256': hashlib.sha256((ROOT / 'next/dist/comani-0.2.0-py3-none-any.whl').read_bytes()).hexdigest(),
              'scheduled_unix_time': time.time(), 'helper_source': str(Path(__file__).resolve())}
    with (ROOT / 'daemon.log').open('ab', buffering=0) as log:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                  '--launch', str(old_pid), '--reserve-gib', str(reserve_gib)],
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 start_new_session=True)
    record['helper_pid'] = child.pid
    (ROOT / 'configured-daemon.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def launch(old_pid, reserve_gib):
    time.sleep(5)  # Let the scheduling RPC finish before graceful shutdown.
    process = Path('/proc') / str(old_pid)
    assert b'comani._runtime' in (process / 'cmdline').read_bytes()
    os.kill(old_pid, signal.SIGTERM)
    for _ in range(200):
        if not process.exists() or (process / 'stat').read_text().split()[2] == 'Z':
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('Previous daemon did not stop; refusing a second daemon')
    print(json.dumps({'configured_daemon_pid': os.getpid(), 'command': command(reserve_gib)}), flush=True)
    (ROOT / 'daemon.pid').write_text(str(os.getpid()))
    os.execv(sys.executable, command(reserve_gib))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', required=True, type=int)
    parser.add_argument('--reserve-gib', type=float, default=4.0)
    options = parser.parse_args()
    launch(options.launch, options.reserve_gib)
