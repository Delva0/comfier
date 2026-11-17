"""Public archive installation with real HTTP, disk publication and subprocesses."""

import asyncio
import hashlib
import io
import json
import stat
import sys
import zipfile
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

import comfier
from comfier import Client, OperationError, PluginChanges
from comfier.daemon.files import FileStore
from comfier.daemon.management import models
from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import ObservedPaths, RuntimeConfig, RuntimeSupervisor

from .test_binary_fetch import serve


def bundle(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return output.getvalue()


@asynccontextmanager
async def installed_server(tmp_path, payload, packages=None, *, source_app=None):
    packages = packages or {"fixture-node": "release/node"}
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path / "comfy"), workspace=str(tmp_path),
                                                runtime_python=sys.executable))
    roots = [tmp_path / "custom_nodes", tmp_path / "secondary_nodes"]
    paths = ObservedPaths({"directories": {key: str(tmp_path / key) for key in ("input", "output", "temp")},
                           "categories": {}, "custom_nodes": list(map(str, roots))})
    supervisor.file_store = FileStore(paths, max_file_size=None)
    supervisor._snapshot_signature = supervisor._paths_signature()
    requests = []
    if source_app is None:
        source_app = web.Application()
        async def download(request):
            requests.append(dict(request.headers))
            return web.Response(body=payload, content_type="application/zip")
        source_app.router.add_get("/archive", download)
    async with serve(source_app) as source_url, serve(create_management_app(supervisor)) as daemon_url:
        manifest = {"url": source_url + "/archive", "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload), "packages": packages}
        yield daemon_url, manifest, supervisor, roots, requests
    assert not list(tmp_path.glob(".comani-plugin-*"))


async def test_public_archive_installs_selected_packages_once_without_importing(tmp_path):
    payload = bundle([(f"release/{name}/__init__.py", "raise RuntimeError('must not import during install')\n")
                      for name in ("first", "second", "ignored")] +
                     [("release/first/LICENSE", "fixture license\n"), ("release/notes.txt", "unselected")])
    async with installed_server(tmp_path, payload, {"one": "release/first/", "two": "release/second"}) as data:
        url, manifest, _, roots, requests = data
        async with Client(url):
            result = await comfier.plugins.install_archive(manifest)
            assert isinstance(result, PluginChanges)
            assert result.restart_required and result.partial_changes
            assert {item.package for item in result.items} == {"one", "two"}
            assert all(item.status == "applied" for item in result.items)
            installed = await comfier.plugins.list()
            assert {item.source for item in installed} == {"archive"}
            assert all(item.archive["sha256"] == manifest["sha256"] for item in installed)
            receipt_path = roots[0] / "one/.comani-archive.json"
            edited = json.loads(receipt_path.read_text())
            edited.update(url="https://user:private-password@example.invalid/release.zip?token=private-query",
                          headers={"Authorization": "private-header"})
            receipt_path.write_text(json.dumps(edited))
            public = repr(await comfier.plugins.list())
            assert all(secret not in public for secret in ("private-password", "private-query", "private-header"))
        assert len(requests) == 1
        assert (roots[0] / "one/LICENSE").read_text() == "fixture license\n"
        assert not (roots[0] / "ignored").exists()
        assert not (roots[0] / "notes.txt").exists()
        assert not roots[1].exists()
        receipt = json.loads((roots[0] / "one/.comani-archive.json").read_text())
        assert receipt["sha256"] == manifest["sha256"]
        assert receipt["subdirectory"] == "release/first"


@pytest.mark.parametrize("mismatch", ["sha256", "size"])
async def test_archive_mismatches_do_not_publish(tmp_path, mismatch):
    payload = bundle([("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}")])
    async with installed_server(tmp_path, payload) as (url, manifest, _, roots, requests):
        manifest[mismatch] = "0" * 64 if mismatch == "sha256" else len(payload) + 1
        async with Client(url) as remote:
            with pytest.raises(OperationError) as failure:
                await remote.plugins.install_archive(manifest)
            assert failure.value.details["result"]["partial_changes"] is False
        assert len(requests) == 1
        assert not (roots[0] / "fixture-node").exists()


@pytest.mark.parametrize("entry", ["/escape.py", "../escape.py", "release/node/../../escape.py",
                                   "C:/escape.py", "release\\escape.py", "symlink", "fifo", "duplicate", "git"])
async def test_unsafe_zip_members_never_publish(tmp_path, entry):
    entries = [("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}")]
    if entry in {"symlink", "fifo"}:
        info = zipfile.ZipInfo("release/node/link")
        info.create_system = 3
        info.external_attr = ((stat.S_IFLNK if entry == "symlink" else stat.S_IFIFO) | 0o777) << 16
        entries.append((info, "../../../escape"))
    else:
        name = "release/node/__INIT__.py" if entry == "duplicate" else "release/node/.git/config" if entry == "git" else entry
        entries.append((name, "unsafe"))
    payload = bundle(entries)
    async with installed_server(tmp_path, payload) as (url, manifest, _, roots, _):
        async with Client(url) as remote:
            with pytest.raises(OperationError) as failure:
                await remote.plugins.install_archive(manifest)
            assert failure.value.details["result"]["partial_changes"] is False
        assert not (roots[0] / "fixture-node").exists()
        assert not (tmp_path / "escape.py").exists()


async def test_existing_name_is_not_overwritten_or_downloaded(tmp_path):
    payload = bundle([("release/node/__init__.py", "new")])
    async with installed_server(tmp_path, payload) as (url, manifest, _, roots, requests):
        occupied = roots[1] / "fixture-node"
        occupied.mkdir(parents=True)
        (occupied / "__init__.py").write_text("keep")
        async with Client(url) as remote:
            with pytest.raises(OperationError):
                await remote.plugins.install_archive(manifest)
        assert requests == []
        assert (occupied / "__init__.py").read_text() == "keep"
        assert not (roots[0] / "fixture-node").exists()


async def test_no_deps_still_executes_installer_and_reports_partial_failure(tmp_path):
    payload = bundle([("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}"),
                      ("release/node/install.py", "from pathlib import Path\nPath('attempted').write_text('yes')\nraise SystemExit(7)\n"),
                      ("release/node/requirements.txt", "comfier-nonexistent-fixture-must-not-fetch==999999\n")])
    async with installed_server(tmp_path, payload) as (url, manifest, _, roots, requests):
        async with Client(url) as remote:
            operation = await remote.plugins.begin_install_archive(manifest, no_deps=True)
            with pytest.raises(OperationError) as failure:
                await operation.result()
            partial = failure.value.details["result"]
            assert partial["partial_changes"] is True
            assert partial["items"][0]["install_script_may_have_run"] is True
        assert len(requests) == 1
        assert (roots[0] / "fixture-node/attempted").read_text() == "yes"


async def test_archive_http_redirect_drops_sensitive_header(tmp_path, monkeypatch):
    payload = bundle([("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}")])
    received = []
    destination = web.Application()
    async def final(request):
        received.append(request.headers.get("Authorization"))
        return web.Response(body=payload)
    destination.router.add_get("/payload", final)
    async with serve(destination) as other_url:
        source = web.Application()
        initial = []
        async def redirect(request):
            initial.append(request.headers.get("Authorization"))
            raise web.HTTPFound(other_url + "/payload")
        source.router.add_get("/archive", redirect)
        monkeypatch.setattr(models, "provider_headers", lambda _: {"Authorization": "Bearer synthetic-archive-fixture"})
        async with installed_server(tmp_path, payload, source_app=source) as (url, manifest, _, roots, _):
            manifest["url"] += "?token=synthetic-private-query"
            async with Client(url) as remote:
                operation = await remote.plugins.begin_install_archive(manifest)
                result = await operation.result()
                assert "synthetic-private-query" not in repr(result)
                status = await operation.status()
                assert "synthetic-private-query" not in repr(status)
            assert (roots[0] / "fixture-node/__init__.py").is_file()
    assert initial == ["Bearer synthetic-archive-fixture"]
    assert received == [None]


async def test_cancellation_cleans_private_archive_stage(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    payload = bundle([("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}")])
    source = web.Application()
    async def download(_):
        started.set()
        await release.wait()
        return web.Response(body=payload)
    source.router.add_get("/archive", download)
    async with installed_server(tmp_path, payload, source_app=source) as (url, manifest, _, roots, _):
        try:
            async with Client(url) as remote:
                operation = await remote.plugins.begin_install_archive(manifest)
                await asyncio.wait_for(started.wait(), 5)
                await operation.cancel()
                assert (await operation.status()).state == "cancelled"
                assert not (roots[0] / "fixture-node").exists()
        finally:
            release.set()


async def test_archive_operation_survives_client_close_and_can_be_reattached(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    payload = bundle([("release/node/__init__.py", "NODE_CLASS_MAPPINGS = {}")])
    source = web.Application()
    requests = []
    async def download(_):
        requests.append(1)
        started.set()
        await release.wait()
        return web.Response(body=payload)
    source.router.add_get("/archive", download)
    async with installed_server(tmp_path, payload, source_app=source) as (url, manifest, _, roots, _):
        remote = await Client(url).start()
        try:
            operation = await remote.plugins.begin_install_archive(manifest)
            handle = operation.export_handle()
            await asyncio.wait_for(started.wait(), 5)
            await remote.close()
            release.set()
            async with Client(url) as observer:
                attached = await observer.operations.get(handle=handle)
                result = await attached.result()
                assert result.items[0].status == "applied"
            assert requests == [1]
            assert (roots[0] / "fixture-node/__init__.py").is_file()
        finally:
            release.set()
            await remote.close()


async def test_later_package_conflict_reports_prior_atomic_publication(tmp_path, monkeypatch):
    payload = bundle([("release/first/__init__.py", "first"), ("release/second/__init__.py", "second")])
    original = models.ModelService._rename_directory
    def introduce_conflict(source, destination, **kwargs):
        if destination.name == "two":
            occupied = tmp_path / "custom_nodes" / "two"
            occupied.mkdir()
            (occupied / "keep").write_text("concurrent owner")
        return original(source, destination, **kwargs)
    monkeypatch.setattr(models.ModelService, "_rename_directory", staticmethod(introduce_conflict))
    async with installed_server(tmp_path, payload, {"one": "release/first", "two": "release/second"}) as data:
        url, manifest, _, roots, requests = data
        async with Client(url) as remote:
            with pytest.raises(OperationError) as failure:
                await remote.plugins.install_archive(manifest)
            partial = failure.value.details["result"]
            assert partial["partial_changes"] is True
            assert [item["package"] for item in partial["items"]] == ["one"]
        assert len(requests) == 1
        assert (roots[0] / "one/__init__.py").read_text() == "first"
        assert (roots[0] / "two/keep").read_text() == "concurrent owner"
        assert not (roots[0] / "two/__init__.py").exists()


async def test_root_swap_at_publication_does_not_follow_replacement_symlink(tmp_path, monkeypatch):
    payload = bundle([("release/node/__init__.py", "fixture")])
    original = models.ModelService._rename_directory
    replaced = False
    def replace_root(source, destination, **kwargs):
        nonlocal replaced
        if kwargs.get("destination_fd") is not None and not replaced:
            replaced = True
            root = tmp_path / "custom_nodes"
            root.rename(tmp_path / "accepted-original")
            outside = tmp_path / "outside"
            outside.mkdir()
            root.symlink_to(outside, target_is_directory=True)
        return original(source, destination, **kwargs)
    monkeypatch.setattr(models.ModelService, "_rename_directory", staticmethod(replace_root))
    async with installed_server(tmp_path, payload) as (url, manifest, _, roots, requests):
        async with Client(url) as remote:
            with pytest.raises(OperationError) as failure:
                await remote.plugins.install_archive(manifest)
            assert failure.value.details["error"]["code"] == "RootsChanged"
            assert failure.value.details["result"]["partial_changes"] is False
        assert len(requests) == 1
        assert not (tmp_path / "outside/fixture-node").exists()
        assert not (tmp_path / "accepted-original/fixture-node").exists()
