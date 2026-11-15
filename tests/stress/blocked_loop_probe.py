"""Independent HTTP server; hold an owned ref while SDK business loop is blocked."""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from comani import Client, SessionClosed

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def snapshot(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + '/_acceptance_snapshot', timeout=5) as response:
        return json.load(response)


def provenance():
    import comani
    package = Path(comani.__file__).resolve().parent
    files = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in package.rglob('*.py')}
    manifest = json.loads(args.manifest.read_text())['files'] if args.manifest else {
        str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (PROJECT / 'src/comani').rglob('*.py')}
    expected = {name.removeprefix('src/comani/'): value for name, value in manifest.items()
                if name.startswith('src/comani/')}
    assert files == expected and '/site-packages/comani' in str(package)
    return {'package': str(package), 'python': sys.executable, 'installed_files': files,
            'loaded_files': {name: {'path': str(Path(module.__file__).resolve()),
                'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
                for name, module in list(sys.modules.items()) if name.startswith('comani')
                and getattr(module, '__file__', '').endswith('.py')}}


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', args.port)), 'occupied; no process touched'
    report = {'client_pid': os.getpid(), 'ttl': args.ttl, 'block_seconds': args.block,
              'before': provenance(), 'expected_survival': args.expect_survival,
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    command = [str(args.server_python), '-I', '-B', str(HERE / 'isolated_load_probe.py'), '--serve',
               '--ttl', str(args.ttl), '--port', str(args.port), '--output', str(args.output)]
    if args.server_manifest:
        command.extend(['--manifest', str(args.server_manifest)])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    url = f'http://127.0.0.1:{args.port}'
    remote = None
    with args.output.with_suffix('.log').open('w') as log:
        child = subprocess.Popen(command, cwd=PROJECT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        report.update(server_pid=child.pid, server_command=command)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'client_pid': os.getpid(), 'server_pid': child.pid, 'url': url}), flush=True)
        try:
            deadline = time.monotonic() + 120
            while True:
                assert child.poll() is None, 'native server exited before readiness'
                try:
                    snapshot(url)
                    break
                except Exception:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.1)
            remote = await Client(url).start()
            call = remote.node('IndependentEcho', value=12345)
            ref = await call.output()
            assert ref.ownership == 'owned' and await ref.fetch() == 12345
            session, ref_id = remote.session_id, ref.id
            report.update(session=session, ref_id=ref_id, before_block=snapshot(url))
            report['block_start'] = time.monotonic()
            time.sleep(args.block)  # Deliberately stop only this SDK business loop.
            report['block_end'] = time.monotonic()
            assert ref.id == ref_id and ref.ownership == 'owned'
            report['after_block_before_fetch'] = snapshot(url)
            try:
                report['value_after_block'] = await ref.fetch()
                report['survived'] = report['value_after_block'] == 12345
            except SessionClosed:
                report['survived'] = False
                report['fetch_error_stack'] = traceback.format_exc()
            report['passed'] = report['survived'] == args.expect_survival
        except BaseException:
            report['harness_error'] = traceback.format_exc()
            report['passed'] = False
            raise
        finally:
            if remote:
                await remote.close(wait=False)
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, 15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(5)
            report['server_exit_code'] = child.wait()
            report['server_reaped'] = not Path(f'/proc/{child.pid}').exists()
            with socket.socket() as probe:
                report['port_free_after'] = probe.connect_ex(('127.0.0.1', args.port)) != 0
            report['after'] = provenance()
            args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report.get(key) for key in ('passed', 'survived', 'client_pid', 'server_pid',
                     'server_exit_code', 'server_reaped', 'port_free_after')}), flush=True)
    return 0 if report['passed'] else 1


parser = argparse.ArgumentParser()
parser.add_argument('--server-python', type=Path, default=PROJECT / '.venv/bin/python')
parser.add_argument('--manifest', type=Path)
parser.add_argument('--server-manifest', type=Path)
parser.add_argument('--ttl', type=float, default=1.5)
parser.add_argument('--block', type=float, default=2.2)
parser.add_argument('--port', type=int, default=18298)
parser.add_argument('--expect-survival', action='store_true', default=True)
parser.add_argument('--expect-expiry', action='store_false', dest='expect_survival')
parser.add_argument('--output', type=Path, required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
