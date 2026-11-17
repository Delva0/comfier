"""Cleanup never acquires or releases an output slot that was not published."""
from types import SimpleNamespace

import pytest

from comfier.daemon.objects import ObjectStore
from comfier.daemon.service import NativeRuntime


@pytest.mark.parametrize('spec,results,port,expected', [
    ({'kind': 'node'}, {}, (None, 0), None),
    ({'kind': 'node'}, {'result': {'outputs': []}}, (None, 0), None),
    ({'kind': 'node'}, {'result': {'outputs': [{'id': 'one'}]}}, (None, 1), None),
    ({'kind': 'node'}, {'result': {'outputs': [{'id': 'one'}]}}, (None, 0), {'id': 'one'}),
    ({'kind': 'workflow', 'graph': {'outputs': []}}, {}, (None, 0), None),
    ({'kind': 'workflow', 'graph': {'outputs': [{}]}}, {}, (None, 0), None),
    ({'kind': 'workflow', 'graph': {'outputs': [{'source': {'$graph_ref': {'node_id': 'a', 'slot': 0}}}]}},
     {'a': {'outputs': []}}, (None, 0), None),
    ({'kind': 'workflow', 'graph': {'outputs': [{'source': {'$graph_ref': {'node_id': 'a', 'slot': 0}}}]}},
     {'a': {'outputs': [{'id': 'graph'}]}}, (None, 0), {'id': 'graph'}),
    ({'kind': 'workflow'}, {'a': {'outputs': []}}, ('a', 0), None),
])
def test_cleanup_locator_handles_only_published_output(spec, results, port, expected):
    runtime = object.__new__(NativeRuntime)
    state = SimpleNamespace(spec=spec, node_results=results)
    assert runtime._port_ref(state, port) == expected


def test_missing_dependency_does_not_abandon_later_valid_leases_or_collection():
    runtime = object.__new__(NativeRuntime)
    runtime.store = ObjectStore('epoch')
    ref = runtime.store.own('session', [41], leases=1)
    runtime.store.release('session', [ref['id']])
    missing = SimpleNamespace(session='session', call_id='missing', spec={'kind': 'node'},
                              node_results={'result': {'outputs': []}}, reservations={(None, 0): 1},
                              forgotten=True, inflight=False, done=SimpleNamespace(done=lambda: True))
    normal = SimpleNamespace(session='session', call_id='normal', spec={'kind': 'node'},
                             node_results={'result': {'outputs': [ref]}}, reservations={(None, 0): 1},
                             forgotten=True, inflight=False, done=SimpleNamespace(done=lambda: True))
    runtime.calls = {('session', 'missing'): missing, ('session', 'normal'): normal}
    consumer = SimpleNamespace(session='session', dependencies_released=False,
                               dependencies=[(missing, (None, 0)), (normal, (None, 0))])
    runtime._release_dependencies(consumer)
    assert not runtime.store.entries and not runtime.calls
    assert missing.reservations[(None, 0)] == normal.reservations[(None, 0)] == 0
    assert consumer.dependencies_released and not consumer.dependencies
    runtime._release_dependencies(consumer)
    assert not runtime.store.entries and not runtime.calls
