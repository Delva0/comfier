"""Reuse the frozen HTTP load workload with native and SDK in separate processes.

RemoteView obtains real registry snapshots over a test-only read-only route.
Explicit fixture-only routes also permit controlled cache eviction and holding
a selected wait response; ordinary pressure/churn never invoke these routes.
"""
import argparse
import asyncio
import contextlib
import gc
import hashlib
import importlib
import importlib.metadata
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, web

from comani.daemon import NativeRuntime
from comani.daemon.http import create_app

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / 'tests/stress'))
workload = importlib.import_module('independent_load')


def provenance():
    import comani
    package = Path(comani.__file__).resolve().parent
    manifest = json.loads(args.manifest.read_text())['files'] if args.manifest else {
        str(path.relative_to(PROJECT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (PROJECT / 'src/comani').rglob('*.py')}
    expected = {name.removeprefix('src/comani/'): value for name, value in manifest.items() if name.startswith('src/comani/')}
    files = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob('*.py')}
    loaded = {name: {'path': str(Path(module.__file__).resolve()), 'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
              for name, module in list(sys.modules.items()) if (name == 'comani' or name.startswith('comani.'))
              and getattr(module, '__file__', '').endswith('.py')}
    source = {name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() for name in manifest}
    assert files == expected, 'installed runtime differs from the tested wheel manifest'
    assert '/site-packages/comani' in str(package)
    assert all(Path(value['path']).is_relative_to(package) for value in loaded.values())
    return {'pid': os.getpid(), 'python': sys.executable, 'package': str(package),
            'version': importlib.metadata.version('comani'), 'installed_files': files,
            'loaded_modules': loaded, 'checkout_files': source,
            'checkout_manifest_differences': [key for key, value in source.items() if value != manifest[key]],
            'head': subprocess.check_output(['git', '-C', str(PROJECT), 'rev-parse', 'HEAD'], text=True).strip(),
            'comfy_commit': subprocess.check_output(['git', '-C', '/home/delva/projects/ComfyUI', 'rev-parse', 'HEAD'], text=True).strip(),
            'workload_sha256': hashlib.sha256(Path(workload.__file__).read_bytes()).hexdigest()}


def rss(pid):
    try:
        fields = Path(f'/proc/{pid}/status').read_text().splitlines()
        return int(next(line.split()[1] for line in fields if line.startswith('VmRSS:'))) / 1024
    except (FileNotFoundError, StopIteration):
        return None


async def lag_monitor(stop, samples):
    while not stop.is_set():
        start = time.monotonic()
        await asyncio.sleep(.02)
        samples.append({'time': time.monotonic(), 'lag_seconds': time.monotonic() - start - .02})


async def serve(args):
    runtime = await NativeRuntime('/home/delva/projects/ComfyUI', cpu=True,
                                  custom_nodes=False, session_ttl=args.ttl).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update({'Independent' + cls.__name__: cls
        for cls in (workload.Echo, workload.Add, workload.Slow, workload.Fail, workload.Lazy,
                    workload.Expand, workload.Output, workload.Pair)})
    events, traces, closes, lags = Counter(), [], [], []
    gated_wait_sessions, gated_waits_entered = set(), set()
    stop = asyncio.Event()
    report = {'before': provenance(), 'ttl': args.ttl, 'pid': os.getpid(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'rpc_observations': traces, 'session_closes': closes, 'loop_lag': lags}
    original_emit, original_close, original_handshake = runtime._emit, runtime.close_session, runtime.handshake

    def emit(kind, state=None, **data):
        events[kind] += 1
        return original_emit(kind, state, **data)

    async def close_session(session_id, **params):
        now = time.monotonic()
        seen = runtime.session_seen.get(session_id)
        closes.append({'time': now, 'session': session_id, 'seen_age': None if seen is None else now - seen,
                       'caller': sys._getframe(1).f_code.co_name,
                       'active_requests': len(runtime.requests.get(session_id, ())),
                       'calls': sum(s == session_id for s, _ in runtime.calls)})
        return await original_close(session_id, **params)

    runtime._emit, runtime.close_session = emit, close_session

    async def handshake(**params):
        start = time.monotonic()
        value = await original_handshake(**params)
        traces.append({'start': start, 'end': time.monotonic(), 'method': 'handshake',
                       'session': value['session_id'], 'seen_after': runtime.session_seen.get(value['session_id']),
                       'status': 200})
        return value

    runtime.handshake = handshake
    app = create_app(runtime)

    @web.middleware
    async def observe(request, handler):
        body = await request.json() if request.path == '/rpc' else {}
        method, params = body.get('method'), body.get('params', {})
        session = params.get('session_id')
        started, seen = time.monotonic(), runtime.session_seen.get(session)
        if method == 'wait_result' and session in gated_wait_sessions:
            gated_waits_entered.add(session)
            while session in runtime.session_seen and not stop.is_set():
                await asyncio.sleep(.02)
        response = await handler(request)
        if method == 'heartbeat' or response.status >= 400:
            traces.append({'start': started, 'end': time.monotonic(), 'method': method, 'session': session,
                           'seen_age_before': None if seen is None else started - seen,
                           'seen_after': runtime.session_seen.get(session), 'status': response.status,
                           'call_id': params.get('call_id'), 'node_id': params.get('node_id'),
                           'error': response.text if response.status >= 400 else None})
        return response

    app.middlewares.insert(0, observe)

    async def snapshot(_request):
        with runtime.store.lock:
            entries = [{'id': key, 'session': entry.session, 'leases': entry.leases}
                       for key, entry in runtime.store.entries.items()]
        return web.json_response({'calls': list(runtime.calls), 'objects': entries,
            'sessions': list(runtime.session_seen), 'closed': len(runtime.closed_sessions),
            'requests': len(runtime.requests), 'queue': runtime.queue.qsize(), 'events': dict(events),
            'gated_waits_entered': sorted(gated_waits_entered)})

    async def evict_native_cache(_request):
        cache = runtime.adapter.executor.caches.outputs.cache
        before = len(cache)
        cache.clear()
        return web.json_response({'cache_entries_before': before, 'cache_entries_after': len(cache)})

    async def hold_wait(request):
        session = (await request.json())['session']
        assert session in runtime.session_seen, 'fixture requires a live server-issued session'
        gated_wait_sessions.add(session)
        return web.json_response({'held_session': session})

    app.router.add_get('/_acceptance_snapshot', snapshot)
    app.router.add_post('/_acceptance_evict_native_cache', evict_native_cache)
    app.router.add_post('/_acceptance_hold_wait', hold_wait)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', args.port).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    lag_task = asyncio.create_task(lag_monitor(stop, lags))
    try:
        await stop.wait()
    finally:
        await lag_task
        report['final_before_shutdown'] = json.loads((await snapshot(None)).text)
        await runner.cleanup()
        report['events'] = dict(events)
        report['after'] = provenance()
        args.output.with_name(args.output.stem + '-server.json').write_text(json.dumps(report, indent=2) + '\n')


class RemoteView:
    def __init__(self, url):
        self.url = url
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.store = self
        self.queue = self
        self.lock = contextlib.nullcontext()

    def snapshot(self):
        with self.opener.open(self.url + '/_acceptance_snapshot', timeout=10) as response:
            return json.load(response)

    @property
    def calls(self):
        return [tuple(key) for key in self.snapshot()['calls']]

    @property
    def entries(self):
        return {entry['id']: SimpleNamespace(**entry) for entry in self.snapshot()['objects']}


    async def join(self):
        while True:
            state = self.snapshot()
            events = state['events']
            if not state['queue'] and events.get('accepted', 0) == events.get('completed', 0) + events.get('failed', 0):
                return
            await asyncio.sleep(.05)


async def client(args):
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', args.port)), 'port occupied; no service touched'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'before': provenance(), 'ttl': args.ttl, 'mode': args.mode, 'sessions': args.sessions, 'rounds': 32,
              'client_pid': os.getpid(), 'rpc_errors': [], 'heartbeats': [], 'loop_lag': [], 'rss_samples': [],
              'explicit_gc': [], 'close_observations': [],
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    original_gc = gc.collect

    def observed_gc(*positional, **keywords):
        start = time.monotonic()
        try:
            return original_gc(*positional, **keywords)
        finally:
            report['explicit_gc'].append({'start': start, 'end': time.monotonic()})

    gc.collect = observed_gc
    command = [str(args.server_python), '-I', '-B', str(Path(__file__).resolve()), '--serve', '--ttl', str(args.ttl),
               '--port', str(args.port), '--sessions', str(args.sessions), '--output', str(args.output)]
    if args.server_manifest:
        command.extend(['--manifest', str(args.server_manifest)])
    url = f'http://127.0.0.1:{args.port}'
    view = RemoteView(url)
    original_transport = workload.TimedTransport
    original_client = workload.Client

    class RecordingClient(original_client):
        async def close(self, **kwargs):
            state = view.snapshot()
            row = {'begin': time.monotonic(), 'session': self.session_id,
                   'active_before_close': self.session_id in state['sessions'],
                   'calls_before_close': sum(key[0] == self.session_id for key in state['calls']),
                   'objects_before_close': sum(item['session'] == self.session_id for item in state['objects'])}
            report['close_observations'].append(row)
            try:
                return await super().close(**kwargs)
            finally:
                row.update(end=time.monotonic(), closed=self.closed)

    class RecordingTransport(original_transport):
        async def request(self, method, **params):
            started = time.monotonic()
            try:
                value = await super().request(method, **params)
            except Exception as error:
                report['rpc_errors'].append({'time': time.monotonic(), 'start': started, 'method': method,
                    'session': params.get('session_id'), 'call_id': params.get('call_id'),
                    'node_id': params.get('node_id'), 'type': type(error).__name__,
                    'error': str(error), 'stack': traceback.format_exc()})
                raise
            if method == 'heartbeat':
                report['heartbeats'].append({'start': started, 'end': time.monotonic(), 'session': params.get('session_id')})
            return value

    workload.TimedTransport = RecordingTransport
    workload.Client = RecordingClient
    meter = SimpleNamespace(runtime=view, phase='isolated_load', rpc=defaultdict(list), waits=0, cancelled=0)
    stop = asyncio.Event()
    monitor_task = None
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    report['threads_before'] = [{'ident': thread.ident, 'name': thread.name} for thread in threading.enumerate()]
    with args.output.with_suffix('.log').open('w') as log:
        child = subprocess.Popen(command, cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        report.update(server_pid=child.pid, server_command=command)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'client_pid': os.getpid(), 'server_pid': child.pid, 'ttl': args.ttl, 'url': url}), flush=True)
        try:
            deadline = time.monotonic() + 120
            while True:
                assert child.poll() is None, 'owned native server exited before readiness'
                try:
                    view.snapshot()
                    break
                except Exception:
                    assert time.monotonic() < deadline, 'owned native readiness timed out'
                    await asyncio.sleep(.1)

            async def monitor():
                while not stop.is_set():
                    report['rss_samples'].append({'time': time.monotonic(), 'sdk_client_mib': rss(os.getpid()),
                                                  'native_server_mib': rss(child.pid)})
                    await asyncio.sleep(.1)

            lag_task = asyncio.create_task(lag_monitor(stop, report['loop_lag']))
            monitor_task = asyncio.create_task(monitor())
            started = time.monotonic()
            if args.mode == 'load':
                rows = await asyncio.wait_for(asyncio.gather(*(workload.load_session(i, 32, url, meter)
                        for i in range(args.sessions)), return_exceptions=True), 300)
                report['rows'] = [row if isinstance(row, dict) else {'unexpected': [repr(row)]} for row in rows]
                report['passed'] = all(isinstance(row, dict) and not row['unexpected']
                    and row['calls'] == 128 and row['remote_calls_after_gc_before_close'] == 0
                    and row['remote_objects_after_gc_before_close'] == 0 for row in rows)
                report['passed'] &= meter.waits >= args.sessions * 128 * 4 and meter.cancelled == args.sessions * 128
            elif args.mode == 'graphs':
                report['graph_details'] = await asyncio.wait_for(workload.large_graph_phase(url, view, meter), 180)
                report['passed'] = True
            else:
                report['completed_sessions'], report['churn_snapshots'] = 0, []

                async def one_session(index):
                    async with workload.Client(transport=RecordingTransport(url, meter)) as remote:
                        stage = 'submit_and_await_output'
                        try:
                            call = remote.node('IndependentEcho', return_call=True, value=index)
                            ref = await call.output()
                            stage = 'fetch'
                            assert await ref.fetch() == index
                        except Exception as error:
                            report.setdefault('churn_failures', []).append({'index': index,
                                'session': remote.session_id, 'stage': stage, 'type': type(error).__name__,
                                'error': str(error), 'stack': traceback.format_exc()})
                            raise

                for milestone in (100, 300, 1000):
                    while report['completed_sessions'] < milestone:
                        first = report['completed_sessions']
                        report['current_batch'] = [first, min(first + 10, milestone)]
                        await asyncio.gather(*(one_session(i) for i in range(*report['current_batch'])))
                        report['completed_sessions'] = report['current_batch'][1]
                    await asyncio.sleep(.05)
                    report['churn_snapshots'].append({'label': f'closed_{milestone}', 'snapshot': view.snapshot()})
                for periods in (1, 3, 10):
                    await asyncio.sleep(args.ttl * periods)
                    report['churn_snapshots'].append({'label': f'additional_{periods}_ttl', 'snapshot': view.snapshot()})
                for entry in report['churn_snapshots']:
                    state = entry['snapshot']
                    assert not state['sessions'] and not state['calls'] and not state['objects']
                    assert state['closed'] <= 64 and state['requests'] == 0
                # A server-minted idle session has no heartbeat or active request.
                # Its observation route does not touch that session's lease.
                async with ClientSession() as http:
                    async with http.post(url + '/rpc', json={'method': 'handshake', 'params': {}}) as response:
                        idle = (await response.json())['result']['session_id']
                idle_start = time.monotonic()
                while idle in view.snapshot()['sessions']:
                    assert time.monotonic() - idle_start < max(3, args.ttl * 4)
                    await asyncio.sleep(.05)
                report['idle_expiry'] = {'session': idle, 'observed_seconds': time.monotonic() - idle_start}
                report['passed'] = report['completed_sessions'] == 1000
            report['seconds'] = time.monotonic() - started
            report['actual_awaits_started'], report['waiters_cancelled'] = meter.waits, meter.cancelled
            report['final_before_server_shutdown'] = view.snapshot()
            report['passed'] &= not report['final_before_server_shutdown']['calls'] and not report['final_before_server_shutdown']['objects']
            executor = getattr(asyncio.get_running_loop(), '_default_executor', None)
            executor_ids = {thread.ident for thread in executor._threads} if executor else set()
            report['business_loop_executor_threads'] = [{'ident': thread.ident, 'name': thread.name}
                for thread in threading.enumerate() if thread.ident in executor_ids]
            report['extra_threads_after_clients_close'] = [{'ident': thread.ident, 'name': thread.name}
                for thread in threading.enumerate() if thread.ident not in baseline_threads | executor_ids]
            report['passed'] &= not report['extra_threads_after_clients_close']
            if args.mode == 'load':
                report['passed'] &= all(row['active_before_close'] and row['calls_before_close'] == 0
                    and row['objects_before_close'] == 0 and row['closed'] for row in report['close_observations'])
        except BaseException:
            report['harness_error'] = traceback.format_exc()
            report['passed'] = False
            raise
        finally:
            stop.set()
            if monitor_task:
                await monitor_task
                await lag_task
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
            server_report = args.output.with_name(args.output.stem + '-server.json')
            if server_report.exists() and args.mode == 'load':
                observed = json.loads(server_report.read_text())
                close_by_session = {row['session']: row for row in report['close_observations']}
                expiries = [row for row in observed['session_closes'] if row['caller'] == '_expire_sessions']
                report['expiry_after_explicit_close_began'] = [row for row in expiries
                    if row['session'] in close_by_session and row['time'] >= close_by_session[row['session']]['begin']
                    and close_by_session[row['session']]['calls_before_close'] == 0
                    and close_by_session[row['session']]['objects_before_close'] == 0]
                report['unexpected_active_expiries'] = [row for row in expiries
                    if row not in report['expiry_after_explicit_close_began']]
                report['passed'] &= not report['unexpected_active_expiries']
            args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report.get(key) for key in ('passed', 'ttl', 'seconds', 'actual_awaits_started', 'waiters_cancelled',
                     'client_pid', 'server_pid', 'server_exit_code', 'server_reaped', 'port_free_after')}), flush=True)
    return 0 if report['passed'] else 1


parser = argparse.ArgumentParser()
parser.add_argument('--serve', action='store_true')
parser.add_argument('--ttl', type=float, default=1.5)
parser.add_argument('--port', type=int, default=18293)
parser.add_argument('--sessions', type=int, default=32)
parser.add_argument('--mode', choices=('load', 'churn', 'graphs'), default='load')
parser.add_argument('--manifest', type=Path)
parser.add_argument('--server-manifest', type=Path)
parser.add_argument('--server-python', type=Path, default=PROJECT / '.venv/bin/python')
parser.add_argument('--output', type=Path, required=True)
if __name__ == '__main__':
    args = parser.parse_args()
    sys.exit(asyncio.run(serve(args) if args.serve else client(args)))
