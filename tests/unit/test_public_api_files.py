"""UI files stay bound to their producing client; no output fetch is needed."""

import pytest

import comfier
from comfier import Client, FileRoot, Files, InProcessTransport, RemoteFile

from .conftest import FakeRuntime
from .test_files_api import running

UI = {
    'save': {'images': [{'filename': 'same.png', 'subfolder': 'nested', 'type': 'output'},
                        {'filename': 'same.png', 'subfolder': '', 'type': 'temp'}]},
    'load': {'images': [{'filename': 'in.png', 'type': 'input'}]},
    'text': {'text': ['not a descriptor']},
}


@pytest.fixture
def stat_calls(monkeypatch):
    calls = []

    async def stat(self, path, *, directory=None, root=None, checksum=False):
        calls.append((self._client, path, directory))
        return RemoteFile(FileRoot(directory, '/native/' + directory, directory, None, 0),
                          path, path, 2, None, (1, 2, 3))

    monkeypatch.setattr(Files, 'stat', stat)
    return calls


async def test_from_ui_all_nodes_order_duplicates_filters_and_directories(peer, stat_calls):
    remote, _ = peer
    values = await comfier.files.from_ui(UI)
    assert isinstance(values, tuple) and len(values) == 3
    assert [(value.relative_path, value.root.directory) for value in values] == [
        ('nested/same.png', 'output'), ('same.png', 'temp'), ('in.png', 'input')]
    assert all(owner is remote for owner, _, _ in stat_calls)
    assert len(await remote.files.from_ui(UI, key='images')) == 3
    assert len(await remote.files.from_ui(UI, node_id='save', key='images')) == 2
    assert len(await remote.files.from_ui(UI, node_id='load')) == 1
    assert await remote.files.from_ui({'node': {'text': ['hello']}}) == ()
    with pytest.raises(KeyError):
        await remote.files.from_ui(UI, node_id='absent')


@pytest.mark.parametrize('descriptor', [
    {'filename': 12, 'type': 'output'}, {'filename': '', 'type': 'output'},
    {'filename': 'x', 'type': 'arbitrary'},
    {'filename': 'x', 'subfolder': None, 'type': 'input'},
])
async def test_bad_standard_descriptors_are_not_silently_ignored(peer, descriptor):
    remote, _ = peer
    with pytest.raises(ValueError, match='descriptor'):
        await remote.files.from_ui({'node': {'images': [descriptor]}})


async def test_call_files_uses_original_owner_even_under_another_scope(peer, stat_calls):
    remote, runtime = peer
    original = runtime.execute

    async def execute(spec, done):
        await original(spec, done)
        done.result()['metadata'] = {'ui': UI}

    runtime.execute = execute
    call = remote.node('Echo', return_call=True, value=1)
    async with Client(transport=InProcessTransport(FakeRuntime())):
        values = await call.files()
        assert len(values) == 3
        assert all(owner is remote for owner, _, _ in stat_calls)
        selected = await call.file(node_id='load', key='images')
        assert selected.relative_path == 'in.png'
        with pytest.raises(ValueError, match='found 3'):
            await call.file()
        with pytest.raises(ValueError, match='found 0'):
            await call.file(node_id='text')


async def test_file_stat_errors_propagate_and_never_fetch_outputs(peer, monkeypatch):
    remote, runtime = peer

    async def no_fetch(*args, **kwargs):
        raise AssertionError('tensor/object fetch should not be used for native files')

    runtime.fetch = no_fetch

    async def fail_stat(*args, **kwargs):
        raise FileNotFoundError('native missing')

    monkeypatch.setattr(Files, 'stat', fail_stat)
    with pytest.raises(FileNotFoundError, match='native missing'):
        await remote.files.from_ui(UI)


async def test_real_ui_paths_validate_and_independent_download_keeps_explicit_owner(tmp_path):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'original instance data')
    async with running(tmp_path / 'a') as (_, url_a):
        async with running(tmp_path / 'b') as (_, url_b):
            async with Client(url_a, token='file-token') as original:
                for directory in ('input', 'temp', 'output'):
                    await original.files.upload(source, directory=directory, path='nested/same.bin')
                values = await comfier.files.from_ui({'node': {'files': [
                    {'filename': 'same.bin', 'subfolder': 'nested', 'type': directory}
                    for directory in ('input', 'temp', 'output')]}})
                assert [value.root.directory for value in values] == ['input', 'temp', 'output']
                async with Client(url_b, token='file-token'):
                    downloaded = await original.files.download(values[-1], tmp_path / 'copied.bin')
                    assert downloaded.local_path.read_bytes() == source.read_bytes()
                    with pytest.raises(comfier.RemoteError):
                        await comfier.files.download(values[-1], tmp_path / 'must-not-exist.bin')
                for path in ('../escape.bin', '/absolute.bin'):
                    with pytest.raises((ValueError, comfier.RemoteError)):
                        await original.files.from_ui({'node': {'images': [
                            {'filename': path, 'type': 'output'}]}})
