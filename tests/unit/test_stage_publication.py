"""Real inode/block facts for internal managed-stage publication."""

import asyncio
import errno
import hashlib
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from comani.daemon.files import FileChangedError, FileStore, Upload, verify_staged
from comani.daemon.management.models import ModelService
from comani.daemon.management.service import ManagementService

from .test_binary_fetch import serve
from .test_management_service import Supervisor, finish, submit
from .test_runtime_files import Folders


def staged(base, data=b"verified bytes"):
    parent = Path(tempfile.mkdtemp(prefix="owned-stage-", dir=base))
    path = parent / "payload.part"
    path.write_bytes(data)
    return verify_staged(path, size=len(data), sha256=hashlib.sha256(data).hexdigest())


def store_at(path, *, max_size=None):
    folders = Folders(path)
    store = FileStore(folders, max_file_size=max_size)
    return store, folders


def identity(value):
    return value.st_dev, value.st_ino


def test_same_filesystem_reuses_inode_and_blocks_with_no_second_payload_allocation(tmp_path, monkeypatch):
    data = bytes(range(251)) * 33000
    stage = staged(tmp_path, data)
    before = stage.path.stat()
    store, folders = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[1]
    original_link, during = os.link, []

    def linked(source, destination, **kwargs):
        original_link(source, destination, **kwargs)
        source_value, target_value = Path(source).stat(), Path(destination).stat()
        during.append((identity(source_value), identity(target_value), source_value.st_nlink,
                       source_value.st_blocks, target_value.st_blocks))

    monkeypatch.setattr(os, "link", linked)
    # A target cannot fit a second payload according to the free-space fact.
    # The actual inode/block observations independently establish that no
    # second data allocation occurs; this is not only a copy-function mock.
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    info = store.publish_staged(root, "nested/model.bin", stage)
    target = root.path / info.relative_path
    after = target.stat()
    assert identity(after) == identity(before)
    assert during == [(identity(before), identity(before), 2, before.st_blocks, before.st_blocks)]
    assert after.st_nlink == 1 and after.st_blocks == before.st_blocks
    assert not stage.path.exists()
    assert target.read_bytes() == data
    assert info.size == len(data) and info.sha256 == hashlib.sha256(data).hexdigest()
    with store.open_download(root, info.relative_path) as downloaded:
        assert downloaded.metadata == info
    assert "checkpoints" not in folders.filename_list_cache


def test_competing_stages_create_exactly_one_file_and_preserve_loser(tmp_path):
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    stages = [staged(tmp_path, b"left"), staged(tmp_path, b"right")]

    def publish(stage):
        try:
            return store.publish_staged(root, "same.bin", stage)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, stages))
    assert sum(result is not None for result in results) == 1
    winner = next(index for index, result in enumerate(results) if result is not None)
    assert identity((root.path / "same.bin").stat()) == (stages[winner].revision.device,
                                                       stages[winner].revision.inode)
    assert not stages[winner].path.exists()
    assert stages[1 - winner].path.read_bytes() in {b"left", b"right"}


def test_late_external_winner_is_not_overwritten_or_removed(tmp_path, monkeypatch):
    stage = staged(tmp_path)
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    original = os.link

    def late_winner(source, destination, **kwargs):
        Path(destination).write_bytes(b"external winner")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", late_winner)
    with pytest.raises(FileExistsError):
        store.publish_staged(root, "existing.bin", stage)
    assert stage.path.exists()
    assert (root.path / "existing.bin").read_bytes() == b"external winner"


@pytest.mark.parametrize("link", ["parent", "target"])
def test_native_symlink_cannot_redirect_stage_publication(tmp_path, link):
    stage = staged(tmp_path)
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    root.path.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.bin"
    protected.write_bytes(b"keep")
    if link == "parent":
        (root.path / "alias").symlink_to(outside, target_is_directory=True)
        path = "alias/model.bin"
    else:
        (root.path / "alias").symlink_to(protected)
        path = "alias"
    with pytest.raises((ValueError, FileExistsError)):
        store.publish_staged(root, path, stage)
    assert stage.path.exists() and protected.read_bytes() == b"keep"
    assert not (outside / "model.bin").exists()


@pytest.mark.parametrize("bad", ["symlink", "parent-symlink", "cache-hardlink", "shared-directory"])
def test_only_private_unshared_regular_stage_is_adoptable(tmp_path, bad):
    owner = Path(tempfile.mkdtemp(dir=tmp_path))
    source = owner / "payload.part"
    other = tmp_path / "cache.bin"
    other.write_bytes(b"persistent cache")
    if bad == "symlink":
        source.symlink_to(other)
    elif bad == "cache-hardlink":
        os.link(other, source)
    elif bad == "parent-symlink":
        source.write_bytes(b"data")
        alias = tmp_path / "alias"
        alias.symlink_to(owner, target_is_directory=True)
        source = alias / source.name
    else:
        source.write_bytes(b"data")
        owner.chmod(0o755)
    with pytest.raises(ValueError):
        verify_staged(source)
    assert other.read_bytes() == b"persistent cache"


@pytest.mark.parametrize("bad", ["overflow", "traversal", "shadow", "changed", "lookup"])
def test_bad_publication_never_clobbers_existing_or_consumes_stage(tmp_path, monkeypatch, bad):
    stage = staged(tmp_path, b"good")
    store, folders = store_at(tmp_path / "native", max_size=3 if bad == "overflow" else None)
    first, second = store.roots(category="checkpoints")
    path = "../outside.bin" if bad == "traversal" else "model.bin"
    if bad == "shadow":
        first.path.mkdir(parents=True)
        (first.path / path).write_bytes(b"existing")
    if bad == "changed":
        stage.path.write_bytes(b"evil")
        os.utime(stage.path, ns=(stage.revision.mtime_ns + 1_000_000,
                                stage.revision.mtime_ns + 1_000_000))
    if bad == "lookup":
        monkeypatch.setattr(folders, "get_full_path", lambda *_: "/different/model.bin")
    with pytest.raises((ValueError, FileExistsError, FileChangedError)):
        store.publish_staged(second, path, stage)
    assert stage.path.exists()
    assert not (second.path / "model.bin").exists()
    if bad == "shadow":
        assert (first.path / path).read_bytes() == b"existing"
    assert not list((tmp_path / "native").rglob(".comani-*.part"))


def force_exdev(monkeypatch, stage):
    original = os.link

    def linked(source, destination, **kwargs):
        if Path(source) == stage.path:
            raise OSError(errno.EXDEV, "injected cross-filesystem stage")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "link", linked)


def test_exdev_fallback_checks_target_space_and_copies_verified_bounded_chunks(tmp_path, monkeypatch):
    stage = staged(tmp_path, b"data" * 700000)
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    original_usage, original_write = shutil.disk_usage, Upload.write
    checked, sizes = [], []

    def usage(directory):
        checked.append(Path(directory))
        return original_usage(directory)

    def write(upload, chunk):
        sizes.append(len(chunk))
        return original_write(upload, chunk)

    force_exdev(monkeypatch, stage)
    monkeypatch.setattr(shutil, "disk_usage", usage)
    monkeypatch.setattr(Upload, "write", write)
    info = store.publish_staged(root, "nested/model.bin", stage)
    target = root.path / info.relative_path
    assert identity(target.stat()) != (stage.revision.device, stage.revision.inode)
    assert checked == [target.parent]
    assert sum(sizes) == stage.size and max(sizes) <= store.chunk_size
    assert hashlib.sha256(target.read_bytes()).hexdigest() == stage.sha256 == info.sha256
    assert not stage.path.exists() and not store._uploads


def test_exdev_low_space_fails_before_allocating_target_copy(tmp_path, monkeypatch):
    stage = staged(tmp_path, b"payload")
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    force_exdev(monkeypatch, stage)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=stage.size - 1))
    with pytest.raises(OSError) as failure:
        store.publish_staged(root, "model.bin", stage)
    assert failure.value.errno == errno.ENOSPC
    assert stage.path.read_bytes() == b"payload"
    assert not (root.path / "model.bin").exists()
    assert not store._uploads and not list(root.path.rglob(".comani-*.part"))


def test_exdev_copy_still_rejects_a_corrupted_whole_file_digest(tmp_path, monkeypatch):
    stage = staged(tmp_path, b"verified payload" * 10000)
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    force_exdev(monkeypatch, stage)
    original = Upload.write

    def corrupt(upload, chunk):
        return original(upload, b"\0" * len(chunk))

    monkeypatch.setattr(Upload, "write", corrupt)
    with pytest.raises(ValueError, match="SHA256"):
        store.publish_staged(root, "corrupt.bin", stage)
    assert stage.path.exists() and not (root.path / "corrupt.bin").exists()
    assert not store._uploads and not list(root.path.rglob(".comani-*.part"))


def test_real_cross_filesystem_copy_preserves_digest(tmp_path):
    shared = Path("/dev/shm")
    if not shared.is_dir() or shared.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("Requires a distinct writable tmpfs filesystem")
    owner = Path(tempfile.mkdtemp(prefix="comani-stage-test-", dir=shared))
    try:
        stage = staged(owner, b"real cross-filesystem payload" * 10000)
        store, _ = store_at(tmp_path / "native")
        root = store.roots(category="checkpoints")[0]
        info = store.publish_staged(root, "cross.bin", stage)
        target = root.path / info.relative_path
        assert target.stat().st_dev != stage.revision.device
        assert hashlib.sha256(target.read_bytes()).hexdigest() == info.sha256 == stage.sha256
        assert not stage.path.exists()
    finally:
        shutil.rmtree(owner)


@pytest.mark.parametrize("snapshot", [False, True])
async def test_model_http_and_snapshot_files_share_adoption_and_native_cache_notification(tmp_path, monkeypatch, snapshot):
    data = b"model content" * 100000
    app = web.Application()

    async def model(_):
        return web.Response(body=data)

    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    supervisor.state = "running"
    notices = []

    async def changed(category):
        notices.append(category)

    supervisor.files_changed = changed
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    supervisor.file_store.folder_paths.filename_list_cache["checkpoints"] = "old"
    acquire, sources = service.models._acquire, []

    async def acquired(*args, **kwargs):
        result = await acquire(*args, **kwargs)
        sources.append(result[0].revision)
        return result

    monkeypatch.setattr(service.models, "_acquire", acquired)
    async with serve(app) as url:
        artifact = {"provider": "url", "source": {"url": url + "/model"}, "filename": "model.bin",
                    "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        arguments = ({"manifest": {"files": [artifact]}, "publish": "files", "path": "family"}
                     if snapshot else {"artifact": artifact, "path": "model.bin"})
        try:
            _, accepted = await submit(service, "models", "snapshot" if snapshot else "download",
                                       {**arguments, "root": root.id, "transport": "http"})
            result = await finish(service, accepted)
            assert result["state"] == "succeeded", result
            item = result["result"]["files"][0] if snapshot else result["result"]
            target = root.path / item["file"]["relative_path"]
            assert identity(target.stat()) == (sources[0].device, sources[0].inode)
            assert item["file"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
            assert item["file"]["revision"] == [target.stat().st_dev, target.stat().st_ino,
                target.stat().st_size, target.stat().st_mtime_ns, target.stat().st_ctime_ns]
            assert item["native_visibility"] == "visible" and notices == ["checkpoints"]
            assert "checkpoints" not in supervisor.file_store.folder_paths.filename_list_cache
            assert not list((tmp_path / ".comani" / "staging").iterdir())
        finally:
            await service.close()


@pytest.mark.parametrize("creation_mask", [0o000, 0o002])
@pytest.mark.parametrize("publish", ["download", "files", "directory"])
async def test_model_acquisition_publishes_private_payload_under_permissive_umask(
    tmp_path, creation_mask, publish,
):
    if not hasattr(os, "getuid"):
        pytest.skip("Requires POSIX ownership and mode checks")
    data = b"private model payload"
    app = web.Application()

    async def model(_):
        return web.Response(body=data)

    app.router.add_get("/model", model)
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    previous_mask = os.umask(creation_mask)
    try:
        async with serve(app) as url:
            artifact = {"provider": "url", "source": {"url": url + "/model"},
                        "filename": "model.bin", "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()}
            arguments = ({"artifact": artifact, "path": "model.bin"} if publish == "download"
                         else {"manifest": {"files": [artifact]}, "publish": publish, "path": "family"})
            _, accepted = await submit(service, "models", "download" if publish == "download" else "snapshot",
                                       {**arguments, "root": root.id, "transport": "http"})
            result = await finish(service, accepted)
            assert result["state"] == "succeeded", result
            target = root.path / ("model.bin" if publish == "download" else "family/model.bin")
            assert target.read_bytes() == data
            assert target.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(previous_mask)
        await service.close()


async def test_cross_filesystem_publication_cancellation_drains_and_cleans_copy(tmp_path, monkeypatch):
    stage = staged(tmp_path, b"x" * (2 * 1024 * 1024 + 3))
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    force_exdev(monkeypatch, stage)
    entered, release = threading.Event(), threading.Event()
    original = Upload.write

    def write(upload, chunk):
        entered.set()
        assert release.wait(3)
        return original(upload, chunk)

    monkeypatch.setattr(Upload, "write", write)
    pending = asyncio.create_task(ModelService._publish_staged(store, root, "cancel.bin", stage))
    assert await asyncio.to_thread(entered.wait, 1)
    pending.cancel()
    try:
        await asyncio.sleep(0.01)
        pending.cancel()
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert stage.path.exists() and not (root.path / "cancel.bin").exists()
    assert not store._uploads and not list(root.path.rglob(".comani-*.part"))


async def test_atomic_link_wins_late_cancellation_and_returns_published_metadata(tmp_path, monkeypatch):
    stage = staged(tmp_path)
    store, _ = store_at(tmp_path / "native")
    root = store.roots(category="checkpoints")[0]
    linked, release = threading.Event(), threading.Event()
    original = os.link

    def link(source, target, **kwargs):
        original(source, target, **kwargs)
        linked.set()
        assert release.wait(3)

    monkeypatch.setattr(os, "link", link)
    pending = asyncio.create_task(ModelService._publish_staged(store, root, "committed.bin", stage))
    assert await asyncio.to_thread(linked.wait, 1)
    pending.cancel()
    try:
        await asyncio.sleep(0.01)
        assert not pending.done()
    finally:
        release.set()
    info = await pending
    assert info.sha256 == stage.sha256
    assert (root.path / info.relative_path).is_file() and not stage.path.exists()
