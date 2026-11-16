"""Python returns a single Any value regardless of the Python value's shape."""

import pytest

import comani
from comani import Call, GraphRef, OutputFuture
from comani.code import prepare_code


@pytest.mark.parametrize('value', [(1, 2), {'items': [1, 2]}, None])
async def test_default_python_single_output_and_explicit_call(peer, value):
    remote, _ = peer
    future = comani.python(lambda item: item, inputs={'item': value})
    assert isinstance(future, OutputFuture) and not isinstance(future, Call)
    assert await (await future).fetch() == value
    call = remote.python(lambda item: item, inputs={'item': value}, return_call=True)
    assert isinstance(call, Call)
    ref, = await call
    assert await ref.fetch() == value
    with comani.graph() as graph:
        symbolic = comani.python(lambda item: item, inputs={'item': value})
        assert isinstance(symbolic, GraphRef)
        graph.output('one', symbolic)
    assert len(graph.outputs) == 1


def test_code_source_and_callable_are_explicit_mutually_exclusive(tmp_path):
    source = tmp_path / 'ops.py'
    source.write_text('def main(value):\n    return value + 1\n')
    inline = prepare_code(code=source.read_text(), entry='main')
    file = prepare_code(source=source, entry='main')
    assert inline.kind == file.kind == 'file' and inline.data == file.data
    for options in [
        {'function': lambda: None, 'code': 'def main(): pass', 'entry': 'main'},
        {'code': 'def main(): pass', 'source': source, 'entry': 'main'},
        {'code': 'def main(): pass'},
        {'code': source, 'entry': 'main'},
        {'function': str(source)},
        {'function': 'def main(): pass'},
    ]:
        with pytest.raises(TypeError):
            prepare_code(**options)
    with pytest.raises(FileNotFoundError):
        prepare_code(source='def main(): return 42', entry='main')
    source_text = prepare_code(code='def main():\n    return captured_name\n', entry='main')
    assert b'captured_name' in source_text.data
    assert source_text.descriptor()['python'] is None


async def test_submit_only_accepts_execution_bindings_and_raw_selections_belong_to_graph(peer):
    remote, runtime = peer
    raw = {'1': {'class_type': 'Echo', 'inputs': {'value': 9}}}
    graph = remote.graph(raw, outputs={'answer': ['1', 0]}, targets=['1'])
    assert graph.sealed and graph.targets == ['1']
    run = remote.submit(graph, return_call=True)
    assert await (await run.output('answer')).fetch() == 'answer'
    assert runtime.submissions[-1][1]['graph']['targets'] == ['1']
    for keyword, value in [('outputs', {}), ('targets', [])]:
        with pytest.raises(TypeError):
            remote.submit(raw, **{keyword: value})
    with pytest.raises(ValueError, match='extra'):
        remote.submit(raw, inputs={'undeclared': 1})
    with pytest.raises(ValueError):
        remote.graph(raw, outputs={'bad': ['missing', 0]})
    with pytest.raises(ValueError):
        remote.graph(raw, targets=['missing'])


async def test_sealed_or_queued_graph_cannot_be_mutated_through_compose(peer):
    remote, runtime = peer
    with remote.graph() as child:
        child.output('value', remote.python(lambda: 42))
    with remote.graph() as sealed:
        sealed.output('value', remote.python(lambda: 7))
    before = sealed.to_wire(remote)
    with pytest.raises(ValueError, match='sealed'):
        sealed.compose(child, {})
    assert sealed.to_wire(remote) == before
    run = remote.submit(sealed, return_call=True)
    with pytest.raises(ValueError, match='sealed'):
        sealed.compose(child, {})
    assert sealed.to_wire(remote) == before
    await run
    assert len(runtime.submissions[-1][1]['graph']['nodes']) == 1
