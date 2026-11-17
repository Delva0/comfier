"""Drop/release the last user owner, then block before control callbacks run."""
import argparse
import asyncio
import contextvars
import gc
import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import weakref
from pathlib import Path

from comfier import Client

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
observations = importlib.import_module('lease_lifecycle_probe')
owners = importlib.import_module('shared_owner_probe')
user_owner = contextvars.ContextVar('acceptance_queued_user_owner', default=None)


async def zero_output_call(url):
    client = await Client(url).start()
    with client.graph() as graph:
        client.node('IndependentOutput', return_call=True, value=92)
    call = client.submit(graph, return_call=True)  # No declared workflow outputs: a zero-output public Call.
    assert await call == ()
    await asyncio.sleep(.05)  # The legitimate user await and its continuations have ended.
    return call, weakref.ref(client), client.session_id


async def make_context_control(url, kind):
    client = await Client(url).start()
    client_weak, session = weakref.ref(client), client.session_id
    if kind == 'context_release':
        call = client.node('IndependentEcho', return_call=True, value=93)
        ref = await call.output()
        del call
        await asyncio.sleep(.05)
        token = user_owner.set(client)
        try:
            ref.release()
        finally:
            user_owner.reset(token)
        return ref, client_weak, session
    with client.graph() as graph:
        client.node('IndependentOutput', return_call=True, value=94)
    call = client.submit(graph, return_call=True)
    assert await call == ()
    await asyncio.sleep(.05)
    token = user_owner.set(client)
    try:
        del call
        gc.collect()
    finally:
        user_owner.reset(token)
    return None, client_weak, session


async def make_cross_client_gc(url):
    other = await Client(url).start()
    victim = await Client(url).start()
    tracked = [(weakref.ref(other), other.session_id), (weakref.ref(victim), victim.session_id)]
    token = user_owner.set(other)
    try:
        del victim
        gc.collect()  # The SDK's GC scheduling must not copy the user's other-Client binding.
    finally:
        user_owner.reset(token)
    return tracked


async def one_case(url, kind, ttl):
    if kind == 'context_cross_client_gc':
        tracked = await make_cross_client_gc(url)
    elif kind.startswith('context_'):
        handle, client_weak, session = await make_context_control(url, kind)
        tracked = [(client_weak, session)]
    elif kind == 'release':
        handle, client_weak, session = await owners.make_one_handle(url, 'ref', 91)
        await asyncio.sleep(.05)
        handle.release()  # Keep this released alias alive throughout the observation.
        tracked = [(client_weak, session)]
    else:
        handle, client_weak, session = await zero_output_call(url)
        del handle
        tracked = [(client_weak, session)]
    gc.collect()
    sessions = [session for _, session in tracked]
    record = {'kind': kind, 'session': sessions[0], 'sessions': sessions,
              'client_alive_before_block': any(client_weak() is not None for client_weak, _ in tracked),
              'block_start': time.monotonic()}
    time.sleep(ttl * 3.5)
    state = observations.snapshot(url)
    record.update(block_end=time.monotonic(),
                  client_alive_after_block=any(client_weak() is not None for client_weak, _ in tracked),
                  session_active_after_block=any(session in state['sessions'] for session in sessions))
    # Only now let the original business loop execute its queued callbacks.
    await asyncio.sleep(0)
    await observations.eventually(lambda: all(client_weak() is None for client_weak, _ in tracked))
    await observations.eventually(lambda: all(session not in observations.snapshot(url)['sessions'] for session in sessions))
    record['collected_after_loop_progress'] = True
    return record


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', args.port)), 'occupied; no service touched'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    url = f'http://127.0.0.1:{args.port}'
    command = [str(args.server_python), '-I', '-B', str(HERE / 'isolated_load_probe.py'), '--serve',
               '--ttl', str(args.ttl), '--port', str(args.port), '--output', str(args.output)]
    if args.server_manifest:
        command.extend(['--manifest', str(args.server_manifest)])
    result = {'pid': os.getpid(), 'before': observations.provenance(args), 'baseline_mode': args.baseline,
              'ttl': args.ttl, 'context_cases': args.contexts, 'cases': [],
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with args.output.with_suffix('.log').open('w') as log:
        child = subprocess.Popen(command, cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        result['server_pid'] = child.pid
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'client_pid': os.getpid(), 'server_pid': child.pid, 'url': url}), flush=True)
        try:
            deadline = time.monotonic() + 120
            while True:
                assert child.poll() is None, 'owned native exited before ready'
                try:
                    observations.snapshot(url)
                    break
                except Exception:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.1)
            baseline_threads, baseline_sockets = {thread.ident for thread in threading.enumerate()}, observations.sockets()
            kinds = ('context_release', 'context_forget', 'context_cross_client_gc') if args.contexts else ('release', 'forget_zero_output')
            for kind in kinds:
                result['cases'].append(await one_case(url, kind, args.ttl))
            correct = all(not row['client_alive_before_block'] and not row['client_alive_after_block']
                          and not row['session_active_after_block'] for row in result['cases'])
            result['no_queued_client_root'] = correct
            result['passed'] = not correct if args.baseline else correct
            await observations.eventually(lambda: not observations.extra_threads(baseline_threads))
            result['extra_keeper_threads'] = [thread.name for thread in observations.extra_threads(baseline_threads)]
            await observations.eventually(lambda: observations.sockets() == baseline_sockets)
            result['socket_fds_before'], result['socket_fds_after'] = baseline_sockets, observations.sockets()
            assert result['socket_fds_before'] == result['socket_fds_after']
        except BaseException:
            result.update(passed=False, harness_error=traceback.format_exc())
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    await asyncio.to_thread(child.wait, 15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(5)
            result['server_exit_code'] = child.wait()
            result['server_reaped'] = not Path(f'/proc/{child.pid}').exists()
            with socket.socket() as probe:
                result['port_free_after'] = probe.connect_ex(('127.0.0.1', args.port)) != 0
            result['after'] = observations.provenance(args)
            native = json.loads(args.output.with_name(args.output.stem + '-server.json').read_text())
            for row in result['cases']:
                row['late_heartbeats_while_business_loop_blocked'] = [event for event in native['rpc_observations']
                    if event.get('session') in row['sessions'] and event['method'] == 'heartbeat'
                    and row['block_start'] + args.ttl / 2 < event['start'] < row['block_end']]
            if not args.baseline:
                result['passed'] &= all(not row['late_heartbeats_while_business_loop_blocked'] for row in result['cases'])
            args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result.get(key) for key in ('passed', 'no_queued_client_root', 'cases', 'harness_error',
                     'extra_keeper_threads', 'server_reaped', 'port_free_after')}), flush=True)
    return 0 if result['passed'] else 1


parser = argparse.ArgumentParser()
parser.add_argument('--manifest', type=Path)
parser.add_argument('--server-manifest', type=Path)
parser.add_argument('--server-python', type=Path, default=PROJECT / '.venv/bin/python')
parser.add_argument('--ttl', type=float, default=.3)
parser.add_argument('--port', type=int, default=18405)
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--contexts', action='store_true')
parser.add_argument('--output', type=Path, required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
