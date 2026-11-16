"""Verify public handles retain a Client without observer-owned strong edges.

The baseline mode records the old weak-Client behavior as negative evidence.
Each factory returns only the requested user handle plus weakrefs/strings. Its
Client, Graph, intermediate Call and temporary refs leave scope before GC.
"""
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
import urllib.request
import weakref
from pathlib import Path

from comani import Client, Expired, Released, SessionClosed

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(HERE))
observations = importlib.import_module('lease_lifecycle_probe')
user_owner = contextvars.ContextVar('acceptance_pending_user_owner', default=None)


async def make_one_handle(url, kind, value):
    client = await Client(url).start()
    client_weak, session = weakref.ref(client), client.session_id
    call = client.node('IndependentSlow' if kind != 'ref' else 'IndependentEcho', return_call=True, value=value,
                       **({'delay': .4} if kind != 'ref' else {}))
    await call._state.accepted  # No sender/payload retention is counted as a user owner.
    if kind == 'call':
        assert not call._state.done.done()
        return call, client_weak, session
    if kind == 'future':
        assert not call._state.done.done()
        return call.output(), client_weak, session
    ref = await call.output()
    assert ref.ownership == 'owned' and await ref.fetch() == value
    return ref, client_weak, session


async def consume(handle, kind):
    if kind == 'call':
        refs = await handle
        return await refs[0].fetch()
    if kind == 'future':
        ref = await handle
        return await ref.fetch()
    return await handle.fetch()


async def retained_case(url, kind, value):
    handle, client_weak, session = await make_one_handle(url, kind, value)
    gc.collect()
    await asyncio.sleep(.05)
    record = {'kind': kind, 'session': session, 'client_alive_with_handle': client_weak() is not None}
    try:
        record['value'] = await consume(handle, kind)
        record['usable'] = record['value'] == value
    except Exception as error:
        record.update(usable=False, error=type(error).__name__ + ': ' + str(error), stack=traceback.format_exc())
    del handle
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    record['client_collected_after_last_handle'] = True
    return record


def fixture_post(url, path, payload=None):
    request = urllib.request.Request(url + path, data=json.dumps(payload or {}).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


async def make_aliases(url):
    client = await Client(url).start()
    call = client.node('IndependentEcho', return_call=True, value=83)
    first = call.output()
    ref = await first
    return (first, call.output(), ref), weakref.ref(client), client.session_id


async def aliases_case(url):
    aliases, client_weak, session = await make_aliases(url)
    last = aliases[1]
    del aliases
    gc.collect()
    assert client_weak() is not None
    assert await consume(last, 'future') == 83
    del last
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    return {'name': 'last_future_alias_keeps_client_until_dropped', 'session': session}


async def release_aliases_case(url):
    aliases, client_weak, session = await make_aliases(url)
    assert client_weak() is not None
    aliases[2].release()
    for index in range(3):
        try:
            await consume(aliases[index], 'ref' if index == 2 else 'future')
        except Released:
            pass
        else:
            raise AssertionError('cell release did not invalidate every future/ref alias')
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    # All three released user aliases are still strongly held at this point.
    return {'name': 'release_revokes_all_cell_aliases_and_cell_client_hold', 'aliases_still_held': len(aliases),
            'session': session}


async def make_call_and_ref(url):
    client = await Client(url).start()
    call = client.node('IndependentEcho', return_call=True, value=84)
    ref = await call.output()
    return call, ref, weakref.ref(client), client.session_id


async def call_release_case(url):
    call, released_ref, client_weak, session = await make_call_and_ref(url)
    released_ref.release()
    gc.collect()
    assert client_weak() is not None and session in observations.snapshot(url)['sessions']
    del call
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    try:
        await released_ref.fetch()
    except Released:
        pass
    else:
        raise AssertionError('released alias became usable after dropping its Call')
    return {'name': 'retained_call_is_an_independent_owner_after_cell_release', 'session': session}


async def make_two_slots(url):
    client = await Client(url).start()
    call = client.node('IndependentPair', return_call=True, value=94)
    first, second = await call
    assert first.id != second.id
    return (first, second), weakref.ref(client), client.session_id


async def sibling_cell_case(url):
    refs, client_weak, session = await make_two_slots(url)
    refs[0].release()
    gc.collect()
    assert client_weak() is not None and await refs[1].fetch() == 95
    refs[1].release()
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    return {'name': 'releasing_one_cell_preserves_sibling_owner_until_last_cell_release', 'session': session}


async def explicit_close_case(url):
    client = await Client(url).start()
    call = client.node('IndependentEcho', return_call=True, value=85)
    future = call.output()
    ref = await future
    session = client.session_id
    await client.close()
    await client.close()
    assert session not in observations.snapshot(url)['sessions']
    for handle, kind in ((call, 'call'), (future, 'future'), (ref, 'ref')):
        try:
            await consume(handle, kind)
        except SessionClosed:
            pass
        else:
            raise AssertionError('public owner overrode an explicit Client close')
    return {'name': 'explicit_close_revokes_all_public_owner_operations', 'session': session}


async def make_context_ref(url):
    async with Client(url) as client:
        ref = await observations.owned(client, 86)
        session = client.session_id
    return ref, weakref.ref(client), session


async def context_case(url):
    ref, client_weak, session = await make_context_ref(url)
    assert session not in observations.snapshot(url)['sessions']
    try:
        await ref.fetch()
    except SessionClosed:
        pass
    else:
        raise AssertionError('retained ref overrode async-with close')
    del ref
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    return {'name': 'async_with_close_overrides_retained_ref', 'session': session}


async def abandon_hung_wait(url):
    client = await Client(url).start()
    session = client.session_id
    fixture_post(url, '/_acceptance_hold_wait', {'session': session})
    call = client.node('IndependentEcho', return_call=True, value=87)
    future = call.output()
    await call._state.accepted
    await observations.eventually(lambda: session in observations.snapshot(url)['gated_waits_entered'])
    assert not future._cell.state.done.done()
    return weakref.ref(client), session, call.id


async def hung_wait_case(url):
    client_weak, session, call_id = await abandon_hung_wait(url)
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    await observations.eventually(lambda: not observations.snapshot(url)['calls'] and not observations.snapshot(url)['objects'])
    return {'name': 'acknowledged_hung_http_watcher_has_no_user_owner_and_does_not_root_client',
            'session': session, 'call_id': call_id}


async def abandon_context_hung_wait(url):
    client = Client(url)
    client_weak = weakref.ref(client)
    token = user_owner.set(client)
    try:
        await client.start()
        assert user_owner.get() is client, 'SDK opening changed the active user Context'
        session = client.session_id
        fixture_post(url, '/_acceptance_hold_wait', {'session': session})
        call = client.node('IndependentEcho', return_call=True, value=89)
        future = call.output()
        await call._state.accepted
        assert user_owner.get() is client, 'SDK await changed the active user Context'
        await observations.eventually(lambda: session in observations.snapshot(url)['gated_waits_entered'])
        assert not future._cell.state.done.done()
    finally:
        user_owner.reset(token)
    return client_weak, session


async def context_hung_wait_case(url):
    client_weak, session = await abandon_context_hung_wait(url)
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    await observations.eventually(lambda: not observations.snapshot(url)['calls'] and not observations.snapshot(url)['objects'])
    return {'name': 'cleared_user_context_is_not_copied_into_internal_tasks_futures_or_hung_watcher', 'session': session}


async def make_borrowed(url):
    client = await Client(url).start()
    with client.graph() as graph:
        symbolic = client.node('IndependentEcho', return_call=True, value=88)
        graph.output('value', symbolic.output())
    run = client.submit(graph)
    ref = await run.output()
    assert ref.ownership == 'borrowed'
    # Graph, symbolic node and public run Call all leave scope here.
    return ref, weakref.ref(client), client.session_id


async def borrowed_case(url):
    ref, client_weak, session = await make_borrowed(url)
    gc.collect()
    assert client_weak() is not None and await ref.fetch() == 88
    eviction = fixture_post(url, '/_acceptance_evict_native_cache')
    assert eviction['cache_entries_after'] == 0
    try:
        await ref.fetch()
    except Expired:
        pass
    else:
        raise AssertionError('shared Client ownership pinned or revived a native borrowed cache entry')
    assert client_weak() is not None and session in observations.snapshot(url)['sessions']
    del ref
    gc.collect()
    await observations.eventually(lambda: client_weak() is None)
    await observations.eventually(lambda: session not in observations.snapshot(url)['sessions'])
    return {'name': 'borrowed_ref_keeps_session_but_native_cache_eviction_remains_expired', 'session': session,
            'native_cache_eviction': eviction}


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
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'retained_cases': [], 'checks': []}
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
            for number, kind in enumerate(('call', 'future', 'ref'), 71):
                result['retained_cases'].append(await retained_case(url, kind, number))
            correct = all(row['client_alive_with_handle'] and row['usable'] for row in result['retained_cases'])
            if not args.baseline and correct:
                baseline_threads = {thread.ident for thread in threading.enumerate()}
                baseline_sockets = observations.sockets()
                for operation in (aliases_case, release_aliases_case, call_release_case, sibling_cell_case, explicit_close_case,
                                  context_case, hung_wait_case, context_hung_wait_case, borrowed_case):
                    result['checks'].append(await operation(url))
                await observations.eventually(lambda: not observations.extra_threads(baseline_threads))
                result['extra_keeper_threads'] = [thread.name for thread in observations.extra_threads(baseline_threads)]
                await observations.eventually(lambda: observations.sockets() == baseline_sockets)
                result['socket_fds_before'], result['socket_fds_after'] = baseline_sockets, observations.sockets()
                assert result['socket_fds_before'] == result['socket_fds_after']
                result['final_before_server_shutdown'] = observations.snapshot(url)
                assert not result['final_before_server_shutdown']['sessions']
                assert not result['final_before_server_shutdown']['calls'] and not result['final_before_server_shutdown']['objects']
            result['shared_ownership_passed'] = correct
            result['baseline_counterexample_reproduced'] = not correct
            result['passed'] = not correct if args.baseline else correct
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
            args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result.get(key) for key in ('passed', 'shared_ownership_passed',
                     'baseline_counterexample_reproduced', 'retained_cases', 'checks', 'harness_error',
                     'server_reaped', 'port_free_after')}), flush=True)
    return 0 if result['passed'] else 1


parser = argparse.ArgumentParser()
parser.add_argument('--manifest', type=Path)
parser.add_argument('--server-manifest', type=Path)
parser.add_argument('--server-python', type=Path, default=PROJECT / '.venv/bin/python')
parser.add_argument('--ttl', type=float, default=1.5)
parser.add_argument('--port', type=int, default=18400)
parser.add_argument('--baseline', action='store_true')
parser.add_argument('--output', type=Path, required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
