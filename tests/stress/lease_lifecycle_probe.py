"""Observe HTTP lease survival, GC, terminal rejection and keeper cleanup.

Requires an installed wheel. The native HTTP server runs in another process;
all thread/socket assertions concern the SDK process, never mocked transports.
"""
import argparse
import asyncio
import gc
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import weakref
from pathlib import Path

from comani import Client, SessionClosed

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def provenance(args):
    import comani
    package = Path(comani.__file__).resolve().parent
    installed = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in package.rglob('*.py')}
    if args.manifest:
        expected = {name.removeprefix('src/comani/'): digest for name, digest
                    in json.loads(args.manifest.read_text())['files'].items() if name.startswith('src/comani/')}
        assert installed == expected
    assert '/site-packages/comani' in str(package)
    return {'package': str(package), 'python': sys.executable, 'installed_files': installed,
            'loaded_modules': {name: {'path': str(Path(module.__file__).resolve()),
                'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
                for name, module in list(sys.modules.items()) if name.startswith('comani')
                and getattr(module, '__file__', '').endswith('.py')}}


def snapshot(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + '/_acceptance_snapshot', timeout=5) as response:
        return json.load(response)


def rpc(url, method, **params):
    request = urllib.request.Request(url + '/rpc', data=json.dumps({'method': method, 'params': params}).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)['result']


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


def extra_threads(baseline):
    executor = getattr(asyncio.get_running_loop(), '_default_executor', None)
    executor_ids = {thread.ident for thread in executor._threads} if executor else set()
    return [thread for thread in threading.enumerate() if thread.ident not in baseline | executor_ids]


async def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'observed state did not converge'
        gc.collect()
        await asyncio.sleep(.05)


async def owned(client, value):
    call = client.node('IndependentEcho', value=value)
    ref = await call.output()
    assert ref.ownership == 'owned' and await ref.fetch() == value
    return ref


def loop_worker(args):
    baseline = {thread.ident for thread in threading.enumerate()}
    retained = []

    async def work():
        client = await Client(args.loop_worker).start()
        retained.extend((client, await owned(client, 987)))

    asyncio.run(work())
    deadline = time.monotonic() + 3
    while any(thread.ident not in baseline for thread in threading.enumerate()) and time.monotonic() < deadline:
        time.sleep(.05)
    extra = [{'name': thread.name, 'ident': thread.ident} for thread in threading.enumerate()
             if thread.ident not in baseline]
    result = {'loop_closed': True, 'retained_client_and_ref': len(retained) == 2,
              'extra_threads_after_loop_close': extra, 'source': provenance(args)}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    assert not extra, 'keeper survived closure of its owning business loop'


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', args.port)), 'occupied; no service touched'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    url = f'http://127.0.0.1:{args.port}'
    command = [str(args.server_python), '-I', '-B', str(HERE / 'isolated_load_probe.py'), '--serve',
               '--ttl', str(args.ttl), '--port', str(args.port), '--output', str(args.output)]
    if args.server_manifest:
        command.extend(['--manifest', str(args.server_manifest)])
    result = {'pid': os.getpid(), 'before': provenance(args), 'checks': [], 'ttl': args.ttl}
    clients, worker = [], None
    with args.output.with_suffix('.log').open('w') as log:
        child = subprocess.Popen(command, cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        result['server_pid'] = child.pid
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'client_pid': os.getpid(), 'server_pid': child.pid, 'url': url}), flush=True)
        try:
            deadline = time.monotonic() + 120
            while True:
                assert child.poll() is None, 'owned native server exited before ready'
                try:
                    snapshot(url)
                    break
                except Exception:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.1)
            baseline_threads, baseline_sockets = {thread.ident for thread in threading.enumerate()}, sockets()

            # Four live sessions keep owned refs across synchronous CPU work.
            refs = []
            for index in range(4):
                remote = await Client(url).start()
                clients.append(remote)
                refs.append(await owned(remote, index))
            block_start = time.monotonic()
            iterations = 0
            while time.monotonic() - block_start < args.ttl * 2.2:
                iterations += 1
            for index, ref in enumerate(refs):
                assert await ref.fetch() == index
            result['checks'].append({'name': 'four_owned_refs_survive_cpu_block',
                                     'seconds': time.monotonic() - block_start, 'iterations': iterations})
            del ref, refs
            gc.collect()
            await eventually(lambda: not snapshot(url)['calls'] and not snapshot(url)['objects'])
            assert len(snapshot(url)['sessions']) == 4
            result['checks'].append({'name': 'gc_clears_objects_and_calls_with_four_sessions_still_open'})
            for remote in clients:
                await remote.close()
                await remote.close()
            clients.clear()
            del remote

            # Shared ownership: the user Ref is an owner even without a Client variable.
            remote = await Client(url).start()
            held_ref = await owned(remote, 444)
            client_weak = weakref.ref(remote)
            dropped_session = remote.session_id
            del remote
            gc.collect()
            assert client_weak() is not None
            assert await held_ref.fetch() == 444
            del held_ref
            gc.collect()
            await eventually(lambda: client_weak() is None)
            await eventually(lambda: dropped_session not in snapshot(url)['sessions'])
            result['checks'].append({'name': 'retained_ref_keeps_client_until_last_owner_disappears'})

            # A server terminal response cannot mint a new session or resubmit.
            remote = await Client(url).start()
            clients.append(remote)
            held_ref = await owned(remote, 555)
            terminal_session = remote.session_id
            accepted = snapshot(url)['events']['accepted']
            rpc(url, 'close_session', session_id=terminal_session)
            time.sleep(args.ttl * 2.2)
            try:
                await held_ref.fetch()
            except SessionClosed:
                pass
            else:
                raise AssertionError('terminal session ref unexpectedly remained usable')
            try:
                await asyncio.wait_for(remote.node('IndependentEcho', value=556).output(), 2)
            except SessionClosed:
                pass
            else:
                raise AssertionError('terminal client accepted a new business call')
            assert terminal_session not in snapshot(url)['sessions']
            assert snapshot(url)['events']['accepted'] == accepted
            await remote.close(wait=False)
            await remote.close(wait=False)
            clients.clear()
            del remote, held_ref
            result['checks'].append({'name': 'terminal_session_does_not_revive_or_resubmit'})

            # Closing a business loop stops its keeper even while local owners remain.
            worker_output = args.output.with_name(args.output.stem + '-loop-worker.json')
            worker_command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--loop-worker', url,
                              '--output', str(worker_output)]
            if args.manifest:
                worker_command.extend(['--manifest', str(args.manifest)])
            worker = subprocess.Popen(worker_command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
            result['loop_worker_pid'] = worker.pid
            worker_deadline = time.monotonic() + 10
            while worker.poll() is None:
                assert time.monotonic() < worker_deadline, 'loop-close worker did not exit'
                await asyncio.sleep(.05)
            assert worker.returncode == 0, 'loop-close worker failed; see log/JSON'
            await eventually(lambda: not snapshot(url)['sessions'], timeout=max(4, args.ttl * 4))
            result['checks'].append({'name': 'closed_business_loop_leaves_no_keeper_thread'})

            await eventually(lambda: not extra_threads(baseline_threads))
            result['extra_threads_after_cleanup'] = [thread.name for thread in extra_threads(baseline_threads)]
            executor = getattr(asyncio.get_running_loop(), '_default_executor', None)
            result['business_loop_executor_threads'] = [thread.name for thread in executor._threads] if executor else []
            result['socket_fds_before'], result['socket_fds_after'] = baseline_sockets, sockets()
            assert result['socket_fds_after'] == baseline_sockets
            result['final_before_server_shutdown'] = snapshot(url)
            assert not result['final_before_server_shutdown']['calls'] and not result['final_before_server_shutdown']['objects']
            result['passed'] = True
        except BaseException:
            result['passed'], result['error'] = False, traceback.format_exc()
        finally:
            for remote in clients:
                try:
                    await remote.close(wait=False)
                except Exception:
                    pass
            for process in (worker, child):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        await asyncio.to_thread(process.wait, 15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)
            result['server_exit_code'] = child.wait()
            result['server_reaped'] = not Path(f'/proc/{child.pid}').exists()
            with socket.socket() as probe:
                result['port_free_after'] = probe.connect_ex(('127.0.0.1', args.port)) != 0
            result['after'] = provenance(args)
            args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result.get(key) for key in ('passed', 'checks', 'error', 'extra_threads_after_cleanup',
                     'socket_fds_before', 'socket_fds_after', 'server_reaped', 'port_free_after')}), flush=True)
    return 0 if result['passed'] else 1


parser = argparse.ArgumentParser()
parser.add_argument('--loop-worker')
parser.add_argument('--manifest', type=Path)
parser.add_argument('--server-manifest', type=Path)
parser.add_argument('--server-python', type=Path, default=PROJECT / '.venv/bin/python')
parser.add_argument('--ttl', type=float, default=1.5)
parser.add_argument('--port', type=int, default=18300)
parser.add_argument('--output', type=Path, required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    if args.loop_worker:
        loop_worker(args)
    else:
        sys.exit(asyncio.run(main(args)))
