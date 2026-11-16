"""A verified transfer commits only to its accepted native directory identity."""

import asyncio
import hashlib
import os
import stat

import pytest
from aiohttp import web

from comani.daemon.files import FileStore
from comani.daemon.management.service import ManagementService

from .test_binary_fetch import serve
from .test_management_service import Supervisor, finish, submit


@pytest.fixture(autouse=True)
def no_leaked_root_handles(monkeypatch):
    accepted = []
    original = FileStore.pin_root

    def pin(store, root):
        identity = original(store, root)
        accepted.append(identity)
        return identity

    monkeypatch.setattr(FileStore, "pin_root", pin)
    yield
    assert all(identity.closed and identity.fd is None for identity in accepted)


class ModelPaths:
    def __init__(self, roots):
        self.folder_names_and_paths = {"checkpoints": (list(map(str, roots)), set())}
        self.filename_list_cache = {}

    def get_folder_paths(self, category):
        if category not in self.folder_names_and_paths:
            raise ValueError("Unknown model category")
        return self.folder_names_and_paths[category][0]

    def get_full_path(self, category, name):
        from pathlib import Path
        for root in self.get_folder_paths(category):
            path = Path(root) / name
            if path.is_file():
                return str(path)


@pytest.mark.parametrize("publish", ["download", "files", "directory"])
@pytest.mark.parametrize("change", ["new_category", "same_category", "reorder", "other_file"])
async def test_download_keeps_verified_bytes_when_accepted_root_is_unchanged(tmp_path, publish, change):
    payload = bytes(range(256)) * 4096
    started, release = asyncio.Event(), asyncio.Event()
    transfers = []

    async def model(_):
        started.set()
        await release.wait()
        transfers.append(len(payload))
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    paths = ModelPaths([tmp_path / "first", tmp_path / "selected"])
    store = supervisor.file_store = FileStore(paths, max_file_size=None)
    selected = store.roots(category="checkpoints")[1]
    service = ManagementService(supervisor)
    try:
        async with serve(app) as url:
            artifact = {"provider": "url", "source": {"url": url + "/model"}, "filename": "model.bin",
                        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            arguments = ({"artifact": artifact, "path": "model.bin"} if publish == "download" else
                         {"manifest": {"files": [artifact]}, "publish": publish, "path": "family"})
            _, accepted = await submit(service, "models", "download" if publish == "download" else "snapshot",
                                       {**arguments, "root": selected.id, "transport": "http"})
            await asyncio.wait_for(started.wait(), 2)
            if change == "new_category":
                paths.folder_names_and_paths["LLM"] = ([str(tmp_path / "LLM")], set())
            elif change == "same_category":
                paths.folder_names_and_paths["checkpoints"][0].insert(0, str(tmp_path / "added"))
            elif change == "reorder":
                paths.folder_names_and_paths["checkpoints"][0].reverse()
            else:
                selected.path.mkdir(parents=True, exist_ok=True)
                (selected.path / "unrelated.bin").write_bytes(b"other publisher")
            supervisor.roots_generation = "changed"
            release.set()
            result = await finish(service, accepted)
            assert transfers == [len(payload)]  # one complete body, even before the publication decision
            assert result["state"] == "succeeded", result
            relative = "model.bin" if publish == "download" else "family/model.bin"
            assert (selected.path / relative).read_bytes() == payload
            assert not (tmp_path / "first" / relative).exists()
            assert not (tmp_path / "added" / relative).exists()
    finally:
        release.set()
        await service.close()


@pytest.mark.parametrize("publish", ["download", "directory"])
@pytest.mark.parametrize("change", ["removed", "moved", "symlink", "replaced", "mode", "owner"])
async def test_download_refuses_changed_selected_root(tmp_path, monkeypatch, publish, change):
    payload = b"verified original destination" * 8192
    started, release = asyncio.Event(), asyncio.Event()

    async def model(_):
        started.set()
        await release.wait()
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    paths = ModelPaths([tmp_path / "selected"])
    store = supervisor.file_store = FileStore(paths, max_file_size=None)
    selected, = store.roots(category="checkpoints")
    selected.path.mkdir(mode=0o755)
    service = ManagementService(supervisor)
    try:
        async with serve(app) as url:
            artifact = {"provider": "url", "source": {"url": url + "/model"}, "filename": "model.bin",
                        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            arguments = ({"artifact": artifact, "path": "model.bin"} if publish == "download" else
                         {"manifest": {"files": [artifact]}, "publish": publish, "path": "family"})
            _, accepted = await submit(service, "models", "download" if publish == "download" else "snapshot",
                                       {**arguments, "root": selected.id, "transport": "http"})
            await asyncio.wait_for(started.wait(), 2)
            if change == "removed":
                paths.folder_names_and_paths["checkpoints"] = ([], set())
            elif change == "moved":
                paths.folder_names_and_paths["checkpoints"] = ([str(tmp_path / "new")], set())
            elif change in {"symlink", "replaced"}:
                selected.path.rename(tmp_path / "original")
                if change == "symlink":
                    selected.path.symlink_to(tmp_path / "original", target_is_directory=True)
                else:
                    selected.path.mkdir(mode=0o755)
            elif change == "mode":
                selected.path.chmod(0o700)
            else:
                # The unprivileged unit runner cannot chown. Exercise an observed
                # UID change while retaining all other actual lstat fields.
                original = os.stat
                def changed_owner(path, *args, **kwargs):
                    value = original(path, *args, **kwargs)
                    if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(selected.path):
                        fields = list(value)
                        fields[stat.ST_UID] += 1
                        return os.stat_result(fields)
                    return value
                monkeypatch.setattr(os, "stat", changed_owner)
            supervisor.roots_generation = "changed"
            release.set()
            result = await finish(service, accepted)
            assert result["state"] == "failed", result
            assert result["error"]["code"] == "RootsChanged", result
            for base in [tmp_path / "selected", tmp_path / "original", tmp_path / "new"]:
                assert not (base / "model.bin").exists()
                assert not (base / "family" / "model.bin").exists()
    finally:
        release.set()
        await service.close()


@pytest.mark.parametrize("publish", ["download", "directory"])
async def test_publication_rechecks_root_after_async_preflight(tmp_path, monkeypatch, publish):
    from comani.daemon.management.models import ModelService
    original = ModelService._revalidate_root

    async def replace_after_check(service, store, identity, generation):
        await original(service, store, identity, generation)
        identity.root.path.chmod(0o700)

    monkeypatch.setattr(ModelService, "_revalidate_root", replace_after_check)
    payload = b"complete body before publication"
    async def model(_):
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    paths = ModelPaths([tmp_path / "selected"])
    store = supervisor.file_store = FileStore(paths, max_file_size=None)
    selected, = store.roots(category="checkpoints")
    selected.path.mkdir(mode=0o755)
    service = ManagementService(supervisor)
    try:
        async with serve(app) as url:
            artifact = {"provider": "url", "source": {"url": url + "/model"}, "filename": "model.bin",
                        "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            arguments = ({"artifact": artifact, "path": "model.bin"} if publish == "download" else
                         {"manifest": {"files": [artifact]}, "publish": publish, "path": "family"})
            _, accepted = await submit(service, "models", "download" if publish == "download" else "snapshot",
                                       {**arguments, "root": selected.id, "transport": "http"})
            result = await finish(service, accepted)
            assert result["state"] == "failed", result
            assert result["error"]["code"] == "RootsChanged", result
            assert not (selected.path / "model.bin").exists()
            assert not (selected.path / "family").exists()
    finally:
        await service.close()


def test_observed_removal_retires_id_but_unrelated_updates_keep_it(tmp_path):
    paths = ModelPaths([tmp_path / "selected"])
    store = FileStore(paths, max_file_size=None)
    original, = store.roots(category="checkpoints")
    paths.folder_names_and_paths["LLM"] = ([str(tmp_path / "LLM")], set())
    store.update_paths(paths)
    assert store.root_by_id(original.id) == original
    paths.folder_names_and_paths["checkpoints"] = ([], set())
    store.update_paths(paths)
    paths.folder_names_and_paths["checkpoints"] = ([str(original.path)], set())
    store.update_paths(paths)
    replacement, = store.roots(category="checkpoints")
    assert replacement.id != original.id
    with pytest.raises(ValueError):
        store.root_by_id(original.id)


def test_open_root_handle_prevents_deleted_directory_identity_reuse(tmp_path):
    from comani.daemon.files import FileChangedError
    paths = ModelPaths([tmp_path / "selected"])
    store = FileStore(paths, max_file_size=None)
    root, = store.roots(category="checkpoints")
    identity = store.pin_root(root)
    try:
        root.path.rmdir()
        root.path.mkdir()
        with pytest.raises(FileChangedError):
            identity.check()
    finally:
        identity.close()


@pytest.mark.parametrize("publish", ["download", "directory"])
async def test_cancellation_closes_accepted_root_handle(tmp_path, publish):
    started, release = asyncio.Event(), asyncio.Event()

    async def model(_):
        started.set()
        await release.wait()
        return web.Response(body=b"cancelled")

    app = web.Application()
    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    selected, = supervisor.file_store.roots(category="checkpoints")
    try:
        async with serve(app) as url:
            artifact = {"provider": "url", "source": {"url": url + "/model"}, "filename": "model.bin"}
            arguments = ({"artifact": artifact, "path": "model.bin"} if publish == "download" else
                         {"manifest": {"files": [artifact]}, "publish": publish, "path": "family"})
            _, accepted = await submit(service, "models", "download" if publish == "download" else "snapshot",
                                       {**arguments, "root": selected.id, "transport": "http"})
            await asyncio.wait_for(started.wait(), 2)
            result = await service.dispatch("management_cancel", accepted, "user")
            assert result["state"] == "cancelled", result
            assert not (selected.path / "model.bin").exists()
            assert not (selected.path / "family").exists()
            release.set()
    finally:
        release.set()
        await service.close()
