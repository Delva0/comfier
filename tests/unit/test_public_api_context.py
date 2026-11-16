"""Independent 0.5 ambient routing and ownership boundary acceptance."""

import asyncio
import contextvars
import gc
import importlib
import inspect
import threading
import weakref

import pytest

import comani
from comani import Client, InProcessTransport, InvalidReference, SessionClosed

from .conftest import FakeRuntime
from .test_management_sdk import managed_server


def client():
    return Client(transport=InProcessTransport(FakeRuntime()))


def test_every_stateful_namespace_exposes_explicit_static_signatures():
    for module, cls, local in [
        (comani.files, comani.Files, set()),
        (comani.models, comani.Models, {'resolve', 'versions', 'manifest'}),
        (comani.plugins, comani.Plugins, {'versions'}),
        (comani.comfyui, comani.ComfyUI, set()),
        (comani.bash, comani.Bash, set()),
        (comani.operations, comani.Operations, set()),
    ]:
        for name, method in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith('_') or name in local:
                continue
            proxy = getattr(module, name)
            assert inspect.isfunction(proxy) and not inspect.iscoroutinefunction(proxy)
            original = inspect.signature(method)
            actual = inspect.signature(proxy)
            expected = {k: v.kind for k, v in original.parameters.items() if k != 'self'}
            assert {k: v.kind for k, v in actual.parameters.items()} == expected, (module, name)
            assert actual.return_annotation is not inspect.Signature.empty, (module, name)
    for name in ('node', 'python', 'graph', 'submit', 'start', 'close'):
        assert inspect.isfunction(getattr(comani, name))
    for name in ('resolve', 'versions', 'manifest'):
        assert not hasattr(comani.Models, name)
    assert not hasattr(comani.Plugins, 'versions')
    importlib.import_module('comani._graph')
    importlib.import_module('comani.daemon.service')
    assert callable(comani.graph)


async def test_unentered_started_and_closed_clients_never_create_ambient_context():
    explicit = client()
    with pytest.raises(RuntimeError, match='No current'):
        comani.node('Echo', value=1)
    await explicit.start()
    with pytest.raises(RuntimeError, match='No current'):
        comani.files.roots()
    assert await (await explicit.node('Echo', value=2)).fetch() == 2
    await explicit.close()
    with pytest.raises(RuntimeError, match='No current'):
        comani.graph()


async def test_nested_scopes_restore_and_explicit_client_remains_explicit():
    async with client() as outer:
        assert await (await comani.node('Echo', value='A')).fetch() == 'A'
        try:
            async with client() as inner:
                assert comani.graph().client is inner
                assert await (await outer.node('Echo', value='explicit A')).fetch() == 'explicit A'
                raise ValueError('exit inner')
        except ValueError:
            pass
        assert comani.graph().client is outer
    with pytest.raises(RuntimeError, match='No current'):
        comani.node('Echo')


async def test_async_io_binds_on_call_and_scope_is_revalidated_before_await(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    async with managed_server(tmp_path / 'a') as (_, _, url_a):
        async with managed_server(tmp_path / 'b') as (_, _, url_b):
            async with Client(url_a):
                pending = comani.bash.run('pwd')
                assert inspect.iscoroutine(pending)
                async with Client(url_b):
                    assert (await pending).stdout.strip() == str(tmp_path / 'a')
                    assert (await comani.bash.run('pwd')).stdout.strip() == str(tmp_path / 'b')
                expired = comani.bash.run('printf must-not-run')
            async with Client(url_b):
                with pytest.raises(RuntimeError, match='scope has exited'):
                    await expired


async def test_tasks_isolate_and_inherited_inner_binding_never_falls_back():
    async def worker(label):
        async with client() as current:
            await asyncio.sleep(0)
            assert comani.graph().client is current
            return await (await comani.node('Echo', value=label)).fetch()

    assert await asyncio.gather(worker('A'), worker('B')) == ['A', 'B']
    release = asyncio.Event()
    async with client() as outer:
        async with client():
            async def inherited():
                await release.wait()
                with pytest.raises(RuntimeError, match='scope has exited'):
                    comani.node('Echo', value='forbidden')
            task = asyncio.create_task(inherited())
        assert comani.graph().client is outer
        release.set()
        await task


async def test_plain_thread_lacks_context_and_to_thread_cannot_use_client_loop():
    def probe():
        try:
            comani.node('Echo', value=1)
        except RuntimeError as error:
            return str(error)
        raise AssertionError('cross-thread operation admitted')

    async with client():
        observed = []
        thread = threading.Thread(target=lambda: observed.append(probe()))
        thread.start()
        thread.join()
        assert 'No current' in observed[0]
        assert "scope's thread" in await asyncio.to_thread(probe)


async def test_same_client_owning_context_reentry_and_concurrent_entry_are_rejected():
    async with client() as current:
        with pytest.raises(RuntimeError, match='more than one owning'):
            await current.__aenter__()
        async def reenter():
            with pytest.raises(RuntimeError, match='more than one owning'):
                await current.__aenter__()
        await asyncio.create_task(reenter())
        assert comani.graph().client is current
        await current.close()
        with pytest.raises((RuntimeError, SessionClosed)):
            comani.node('Echo')


async def test_copied_binding_does_not_keep_closed_client_alive():
    current = client()
    await current.__aenter__()
    inherited = contextvars.copy_context()
    reference = weakref.ref(current)
    await current.__aexit__(None, None, None)
    del current
    gc.collect()
    assert reference() is None
    with pytest.raises(RuntimeError, match='scope has exited'):
        inherited.run(comani.graph)


async def test_copied_context_cannot_exit_and_destroy_original_owner():
    async with client() as current:
        async def invalid_exit():
            with pytest.raises((RuntimeError, ValueError)):
                await current.__aexit__(None, None, None)
        await asyncio.create_task(invalid_exit())
        assert not current.closed
        assert comani.graph().client is current
        assert await (await comani.node('Echo', value='still valid')).fetch() == 'still valid'


async def test_graph_receiver_and_ambient_client_cannot_cross_route():
    async with client() as first:
        with comani.graph() as graph:
            port = comani.node('Echo', value='A')
            async with client() as second:
                with pytest.raises(InvalidReference, match='another client'):
                    comani.node('Echo', value='B')
                with pytest.raises(InvalidReference, match='another client'):
                    second.submit(graph)
                assert first.node('Echo', value='explicit A').graph is graph
            graph.output('value', port)
        assert not hasattr(graph, 'bind')
        assert await (await comani.submit(graph, return_call=True).output('value')).fetch() == 'value'
        async with client() as second:
            with second.graph() as another:
                with pytest.raises(InvalidReference, match='another client'):
                    another.compose(graph, {})
