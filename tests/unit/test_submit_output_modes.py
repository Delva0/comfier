"""Independent submit return modes, ownership and completion-barrier acceptance."""

import asyncio
import gc
import inspect

import pytest

import comani
from comani import Call, Client, GraphRef, InProcessTransport, OutputFuture, RemoteError

from ._helpers import eventually
from .conftest import FakeRuntime

NAMES = ('zeta', 'alpha', 'middle')


class WorkflowRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.workflow_gate = asyncio.Event()
        self.workflow_gate.set()
        self.started, self.completed, self.early_results = [], [], {}
        self.fail_tail = False

    async def execute(self, spec, done):
        if spec['kind'] != 'workflow':
            return await super().execute(spec, done)
        call_id = next(key for key, value in self.results.items() if value is done)
        self.started.append(call_id)
        self.early_results[call_id] = {'outputs': [self.info('early', 'borrowed')]}
        try:
            await self.workflow_gate.wait()
            if self.fail_tail:
                raise ValueError('tail failed after export became ready')
            bindings = {key: await self.resolve(value) for key, value in spec['bindings'].items()}
            values = [item['name'] if not bindings else {'name': item['name'], **bindings}
                      for item in spec['graph']['outputs']]
            done.set_result({'outputs': [self.info(value, 'borrowed') for value in values],
                             'metadata': {'executed': list(spec['graph']['nodes']), 'ui': {}}})
            self.completed.append(call_id)
        except Exception as error:
            done.set_exception(error)

    async def wait_result(self, session_id, call_id, node_id=None):
        if node_id is not None:
            await eventually(lambda: call_id in self.early_results)
            return self.early_results[call_id]
        return await super().wait_result(session_id, call_id)


@pytest.fixture
async def workflow_peer():
    runtime = WorkflowRuntime()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        yield remote, runtime


def make_graph(remote, count, *, input_name=None):
    with remote.graph() as graph:
        value = graph.input(input_name) if input_name else 'value'
        for name in NAMES[:count]:
            graph.output(name, remote.node('Echo', value=value))
        if count == 0:
            graph.target(remote.node('Echo', value=value))
    return graph


@pytest.mark.parametrize('count', [0, 1, 3])
@pytest.mark.parametrize('explicit_call', [False, True])
async def test_declared_export_order_zero_one_many_and_awaited_shape(workflow_peer, count, explicit_call):
    remote, runtime = workflow_peer
    graph = make_graph(remote, count)
    kwargs = {'return_call': True} if explicit_call else {}
    result = comani.submit(graph, **kwargs)
    if explicit_call:
        assert isinstance(result, Call)
        ready = await result
        assert isinstance(ready, tuple) and len(ready) == count
    elif count == 0:
        assert result is None and not inspect.isawaitable(result)
        await eventually(lambda: len(runtime.completed) == 1)
        ready = ()
    elif count == 1:
        assert isinstance(result, OutputFuture) and not isinstance(result, Call)
        ready = (await result,)
    else:
        assert isinstance(result, tuple) and len(result) == count
        assert all(isinstance(output, OutputFuture) for output in result)
        ready = await result
    assert [await ref.fetch() for ref in ready] == list(NAMES[:count])
    assert len(runtime.submissions) == 1


async def test_zero_export_raw_and_capture_execute_without_any_public_handle(workflow_peer):
    remote, runtime = workflow_peer
    runtime.workflow_gate.clear()
    raw = {'effect': {'class_type': 'Effect', 'inputs': {'value': 'raw side effect'}}}
    assert remote.submit(raw) is None
    assert comani.submit(make_graph(remote, 0)) is None
    gc.collect()
    await eventually(lambda: len(runtime.started) == 2)
    assert not runtime.completed
    runtime.workflow_gate.set()
    await eventually(lambda: len(runtime.completed) == 2)
    assert len(runtime.submissions) == 2


async def test_context_close_waits_for_zero_export_submission_with_no_call_owner():
    runtime = WorkflowRuntime()
    runtime.workflow_gate.clear()

    async def owner():
        async with Client(transport=InProcessTransport(runtime)) as remote:
            assert remote.submit(make_graph(remote, 0)) is None

    task = asyncio.create_task(owner())
    await eventually(lambda: len(runtime.started) == 1)
    gc.collect()
    await asyncio.sleep(0)
    assert not task.done() and not runtime.completed
    runtime.workflow_gate.set()
    await task
    assert len(runtime.completed) == 1


@pytest.mark.parametrize('count', [1, 3])
async def test_default_exports_feed_eager_inputs_without_call_or_intermediate_fetch(workflow_peer, count):
    remote, runtime = workflow_peer
    result = remote.submit(make_graph(remote, count))
    outputs = (result,) if count == 1 else result
    consumer = comani.python(lambda values: tuple(values), inputs={'values': list(outputs)})
    assert await (await consumer).fetch() == tuple(NAMES[:count])
    assert [spec['kind'] for _, spec in runtime.submissions] == ['workflow', 'node']


@pytest.mark.parametrize('count', [0, 1, 3])
async def test_capture_composes_default_symbolic_exports_without_compute_rpc(workflow_peer, count):
    remote, runtime = workflow_peer
    child = make_graph(remote, count)
    with comani.graph() as outer:
        projected = comani.submit(child)
        if count == 0:
            assert projected is None
        else:
            outputs = (projected,) if count == 1 else projected
            assert all(isinstance(output, GraphRef) and output.graph is outer for output in outputs)
            ready = await projected
            assert ready is projected if count == 1 else all(a is b for a, b in zip(ready, outputs))
            for name, output in zip(NAMES[:count], outputs):
                outer.output(name, output)
        assert len(outer.nodes) == max(1, count)
        assert not runtime.submissions
    result = remote.submit(outer, return_call=True)
    assert [await ref.fetch() for ref in await result] == list(NAMES[:count])
    assert len(runtime.submissions) == 1


async def test_repeated_submissions_have_independent_binding_and_output_owners(workflow_peer):
    remote, runtime = workflow_peer
    graph = make_graph(remote, 1, input_name='tag')
    first = comani.submit(graph, inputs={'tag': 'first'})
    second = remote.submit(graph, inputs={'tag': 'second'})
    first_ref, second_ref = await asyncio.gather(first, second)
    assert first_ref.id != second_ref.id
    assert await first_ref.fetch() == {'name': 'zeta', 'tag': 'first'}
    assert await second_ref.fetch() == {'name': 'zeta', 'tag': 'second'}
    first_ref.release()
    assert await second_ref.fetch() == {'name': 'zeta', 'tag': 'second'}
    assert len(runtime.submissions) == 2


async def test_export_future_and_downstream_preserve_whole_workflow_tail_failure(workflow_peer):
    remote, runtime = workflow_peer
    runtime.workflow_gate.clear()
    runtime.fail_tail = True
    graph = make_graph(remote, 1)
    source = graph.outputs['zeta']
    projected = remote.submit(graph)
    explicit = comani.submit(graph, return_call=True)
    downstream = remote.node('Echo', value=projected)
    default_waiter = asyncio.ensure_future(projected)
    explicit_waiter = asyncio.ensure_future(explicit.output('zeta'))
    early = await explicit[source]
    assert await early.fetch() == 'early'
    await asyncio.sleep(0)
    assert not default_waiter.done() and not explicit_waiter.done()
    runtime.workflow_gate.set()
    for output in (default_waiter, explicit_waiter, downstream, explicit):
        with pytest.raises(RemoteError, match='tail failed after export'):
            await output


@pytest.mark.parametrize('value', [None, 0, 1, 'yes'])
async def test_submit_return_call_is_an_explicit_bool(workflow_peer, value):
    remote, runtime = workflow_peer
    graph = make_graph(remote, 1)
    with pytest.raises(TypeError, match='bool'):
        remote.submit(graph, return_call=value)
    with pytest.raises(TypeError, match='bool'):
        comani.submit(graph, return_call=value)
    assert not runtime.submissions
    assert inspect.signature(Client.submit).parameters['return_call'].default is False
    assert inspect.signature(comani.submit).parameters['return_call'].default is False
