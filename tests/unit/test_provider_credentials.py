"""Provider credential aliases and real loopback redirect boundaries."""

import hashlib
import json

import pytest
from aiohttp import web

import comani
from comani import _model_providers
from comani._model_providers import ProviderQueries, provider_headers
from comani.daemon.management import models, transports
from comani.daemon.management.processes import child_env
from comani.daemon.management.service import ManagementService

from .test_binary_fetch import serve
from .test_management_service import Supervisor, finish, submit


async def test_civitai_api_token_alias_and_explicit_local_query_are_private(monkeypatch):
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    monkeypatch.setenv("CIVITAI_API_TOKEN", "alias-private")
    seen = []

    async def metadata(self, url):
        seen.append(provider_headers(url, token=self.token))
        return {"id": 123, "modelId": 4, "files": [
            {"id": 12, "name": "toy.bin", "sizeKB": 1, "hashes": {"SHA256": "a" * 64}}]}

    monkeypatch.setattr(ProviderQueries, "json", metadata)
    source = await comani.models.resolve({"version_id": 123, "file_id": 12})
    assert seen[-1] == {"Authorization": "Bearer alias-private"}
    await comani.models.resolve({"version_id": 123}, token="explicit-private")
    assert seen[-1] == {"Authorization": "Bearer explicit-private"}
    monkeypatch.setenv("CIVITAI_TOKEN", "legacy-private")
    assert provider_headers("https://www.civitai.com/api/v1/models/4") == {
        "Authorization": "Bearer legacy-private"}
    assert provider_headers("https://civitai.com.example.org/file") == {}
    assert provider_headers("https://example.org/file", token="explicit-private") == {}
    for private in ("alias-private", "explicit-private", "legacy-private"):
        assert private not in repr(source)
        assert private not in json.dumps(source.to_public_dict())
        assert private not in json.dumps(source.files[0].to_wire())


def test_civitai_alias_diagnostics_redact_and_children_do_not_inherit(monkeypatch):
    monkeypatch.setenv("CIVITAI_API_TOKEN", "alias-private")
    monkeypatch.setenv("CIVITAI_TOKEN", "legacy-private")
    result = transports.diagnostic({"stderr": "alias-private legacy-private https://example.org/f?token=secret"})
    assert "alias-private" not in result and "legacy-private" not in result and "secret" not in result
    assert "CIVITAI_API_TOKEN" not in child_env()
    assert "CIVITAI_TOKEN" not in child_env()


async def test_metadata_cross_origin_redirect_drops_authorization(monkeypatch):
    seen = []

    async def target(request):
        seen.append(request.headers.get("Authorization"))
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/file", target)
    async with serve(app) as destination:
        async def initial(request):
            seen.append(request.headers.get("Authorization"))
            raise web.HTTPFound(destination + "/file")

        origin = web.Application()
        origin.router.add_get("/file", initial)
        async with serve(origin) as address:
            monkeypatch.setattr(_model_providers, "provider_headers", lambda url, **kw: {
                "Authorization": "Bearer metadata-private"})
            assert await ProviderQueries().json(address + "/file") == {"ok": True}
    assert seen == ["Bearer metadata-private", None]


@pytest.mark.parametrize("transport", ["auto", "aria2"])
async def test_authenticated_aria2_never_forwards_provider_token_to_redirect(tmp_path, monkeypatch, transport):
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    monkeypatch.setenv("CIVITAI_API_TOKEN", "daemon-private")
    # The guard runs before starting aria2; the test needs no optional binary.
    monkeypatch.setattr(transports.shutil, "which", lambda name: "/unused/aria2c")
    payload = b"private provider model fixture"
    seen = []

    async def target(request):
        seen.append(("target", request.headers.get("Authorization")))
        return web.Response(body=payload)

    destination = web.Application()
    destination.router.add_get("/model", target)
    async with serve(destination) as destination_url:
        async def initial(request):
            seen.append(("origin", request.headers.get("Authorization")))
            raise web.HTTPFound(destination_url + "/model")

        origin = web.Application()
        origin.router.add_get("/model", initial)
        async with serve(origin) as origin_url:
            def scoped_headers(url):
                return provider_headers("https://civitai.com") if url == origin_url + "/model" else {}

            monkeypatch.setattr(models, "provider_headers", scoped_headers)
            monkeypatch.setattr(transports, "provider_headers", scoped_headers)
            supervisor = Supervisor(tmp_path)
            service = ManagementService(supervisor)

            async def address(artifact):
                return origin_url + "/model"

            monkeypatch.setattr(service.models.resolver, "address", address)
            root, = supervisor.file_store.roots(category="checkpoints")
            artifact = {"provider": "civitai", "source": {"version_id": 123, "file_id": 12},
                        "filename": "toy.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            try:
                _, accepted = await submit(service, "models", "download", {
                    "artifact": artifact, "root": root.id, "transport": transport})
                result = await finish(service, accepted)
                assert result["state"] == "succeeded", result
                assert result["result"]["transport"] == "http"
                assert result["progress"]["transport_fallback"]["reason"] == "UnsafeDownloadTransport"
                assert seen == [("origin", "Bearer daemon-private"), ("target", None)]
                assert (root.path / "toy.bin").read_bytes() == payload
                record = service.operations.get(accepted["operation_id"], accepted["daemon_epoch"], "user")
                assert "daemon-private" not in json.dumps(result)
                assert "daemon-private" not in str(list(record.logs))
            finally:
                await service.close()
