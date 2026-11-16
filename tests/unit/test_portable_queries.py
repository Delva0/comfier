"""Pure provider facts and private portable download transport acceptance."""

import json

import pytest
from aiohttp import web

import comani
from comani import Client, InProcessTransport, ModelArtifact
from comani._model_providers import ProviderQueries, provider_headers
from comani.daemon.management.service import ManagementService
from comani.management import _plain

from .conftest import FakeRuntime
from .test_binary_fetch import serve
from .test_management_service import Supervisor, finish, submit


@pytest.fixture
def provider_facts(monkeypatch):
    requests = []

    async def metadata(self, url):
        requests.append((url, provider_headers(url, token=self.token)))
        if '/refs' in url:
            return {'branches': [{'name': 'main', 'targetCommit': 'a' * 40}]}
        if 'huggingface.co' in url:
            return {'sha': 'a' * 40, 'siblings': [
                {'rfilename': 'unet/model.bin', 'lfs': {'size': 16, 'sha256': 'b' * 64}},
                {'rfilename': 'README.md', 'size': 8, 'blobId': 'c' * 40}]}
        if 'civitai.com' in url:
            return {'id': 123, 'modelId': 4, 'files': [
                {'id': 12, 'name': 'toy.safetensors', 'sizeKB': 2,
                 'hashes': {'SHA256': 'D' * 64}, 'downloadUrl': 'https://invalid/?token=never-public'}]}
        if 'api.comfy.org' in url:
            return [{'version': '1.2.3'}]
        raise AssertionError(url)

    monkeypatch.setattr(ProviderQueries, 'json', metadata)
    monkeypatch.setenv('HF_TOKEN', 'local-hf-private')
    monkeypatch.setenv('CIVITAI_TOKEN', 'local-civitai-private')
    return requests


async def test_hf_civitai_manifest_versions_registry_are_local_inside_and_outside_client(provider_facts):
    async def query_all():
        hf = await comani.models.resolve({'repo_id': 'owner/repo'})
        cv = await comani.models.resolve({'version_id': 123})
        manifest = await comani.models.manifest({'repo_id': 'owner/repo'}, allow=['unet/*'])
        versions = await comani.models.versions({'repo_id': 'owner/repo'})
        registry = await comani.plugins.versions('example-package')
        assert hf.revision == 'a' * 40 and hf.files[0].size == 16
        assert cv.files[0].source['version_id'] == 123
        assert cv.files[0].sha256 == 'd' * 64
        assert manifest.total_size == 16 and len(manifest.files) == 1
        assert versions.repo_id == 'owner/repo'
        assert registry.versions == [{'version': '1.2.3'}]
        public = json.dumps([hf.to_public_dict(), cv.to_public_dict(), manifest.to_public_dict(),
                             _plain(hf), _plain(cv), _plain(manifest)])
        assert 'local-hf-private' not in public and 'local-civitai-private' not in public
        assert 'never-public' not in public and 'source_id' not in public

    await query_all()
    runtime = FakeRuntime()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        async def forbidden(*args, **kwargs):
            raise AssertionError('local query attempted a daemon request')
        remote.transport.request = forbidden
        await query_all()
        assert not runtime.submissions
    assert any(header == {'Authorization': 'Bearer local-hf-private'} for _, header in provider_facts)
    assert any(header == {'Authorization': 'Bearer local-civitai-private'} for _, header in provider_facts)
    assert all(header == {} for url, header in provider_facts if 'api.comfy.org' in url)


async def test_explicit_query_token_is_local_and_never_in_descriptor(provider_facts):
    source = await comani.models.resolve({'repo_id': 'owner/repo'}, token='explicit-private')
    assert provider_facts[-1][1] == {'Authorization': 'Bearer explicit-private'}
    assert 'explicit-private' not in repr(source)
    assert 'explicit-private' not in json.dumps(_plain(source))


async def test_generic_signed_url_portable_public_redaction_and_real_daemon_download(tmp_path, monkeypatch):
    payload = b'independent portable model toy'
    seen = []

    async def model(request):
        seen.append((dict(request.query), request.headers.get('Authorization')))
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get('/toy.bin', model)
    monkeypatch.setenv('HF_TOKEN', 'local-hf-do-not-send')
    monkeypatch.setenv('CIVITAI_TOKEN', 'local-cv-do-not-send')
    async with serve(app) as url:
        source = await comani.models.resolve(url + '/toy.bin?signature=caller-explicit-secret')
        artifact, = source.files
        assert isinstance(artifact, ModelArtifact)
        assert 'caller-explicit-secret' not in repr(source)
        assert 'caller-explicit-secret' not in repr(artifact)
        assert 'caller-explicit-secret' not in json.dumps(source.to_public_dict())
        assert artifact.to_wire()['source']['url'].endswith('signature=caller-explicit-secret')
        assert 'source_id' not in json.dumps(artifact.to_wire())
        supervisor = Supervisor(tmp_path)
        service = ManagementService(supervisor)
        root, = supervisor.file_store.roots(category='checkpoints')
        try:
            _, accepted = await submit(service, 'models', 'download', {
                'artifact': _plain(artifact), 'root': root.id, 'transport': 'http'})
            result = await finish(service, accepted)
            assert result['state'] == 'succeeded', result
            assert (root.path / 'toy.bin').read_bytes() == payload
            assert seen == [({'signature': 'caller-explicit-secret'}, None)]
            assert 'caller-explicit-secret' not in json.dumps(result)
            record = service.operations.get(accepted['operation_id'], accepted['daemon_epoch'], 'user')
            assert 'caller-explicit-secret' not in str(list(record.logs))
        finally:
            await service.close()


@pytest.mark.parametrize('source', ['https://user:secret@example.com/x?query=secret',
                                   'not-a-url?secret=value'])
async def test_bad_generic_source_errors_do_not_echo_secret(source):
    with pytest.raises(Exception) as failure:
        await comani.models.resolve(source)
    assert 'secret' not in str(failure.value)
