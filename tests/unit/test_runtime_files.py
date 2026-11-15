"""Real temporary-directory checks for the internal, transport-neutral file layer."""

import asyncio
import errno
import hashlib
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from comani._runtime.files import (
    FileChangedError,
    FileStore,
    UnsupportedFileSystem,
)


class Folders:
    def __init__(self, base):
        self.input = base / "input"
        self.output = base / "output"
        self.temp = base / "temp"
        self.models = {"checkpoints": [base / "models-a", base / "models-b"]}
        self.filename_list_cache = {"checkpoints": "cached-before-upload"}

    def get_input_directory(self):
        return str(self.input)

    def get_output_directory(self):
        return str(self.output)

    def get_temp_directory(self):
        return str(self.temp)

    def get_folder_paths(self, category):
        return [str(path) for path in self.models[category]]

    def get_full_path(self, category, relative_path):
        for path in self.models[category]:
            target = path / relative_path
            if target.is_file():
                return str(target)
        return None


@pytest.fixture
def files(tmp_path):
    folders = Folders(tmp_path)
    return FileStore(folders, max_file_size=64, chunk_size=4), folders


def put(store, root, path="photo.png", data=b"abcdefghij"):
    chunks = (data[index:index + store.chunk_size]
              for index in range(0, len(data), store.chunk_size))
    return store.upload(root, path, chunks, size=len(data),
                        sha256=hashlib.sha256(data).hexdigest())


def parts(base):
    return list(base.rglob(".comani-*.part"))


@pytest.mark.parametrize(("directory", "native_name"), [
    ("input", "photo.png"), ("output", "photo.png [output]"), ("temp", "photo.png [temp]"),
])
def test_upload_and_download_real_chunks(files, directory, native_name):
    store, folders = files
    root, = store.roots(directory=directory)
    info = put(store, root)
    assert info.relative_path == "photo.png" and info.native_name == native_name
    assert info.size == 10 and info.sha256 == hashlib.sha256(b"abcdefghij").hexdigest()
    assert (root.path / info.relative_path).read_bytes() == b"abcdefghij"
    assert not parts(root.path)
    with store.open_download(root, info.relative_path) as download:
        assert download.metadata == info
        chunks = list(download.chunks())
        assert [len(chunk) for chunk in chunks] == [4, 4, 2]
        assert b"".join(chunks) == b"abcdefghij"
    assert download._stream.closed
    store.close()
    # Completed files belong to the shared native filesystem, not a store/session.
    replacement = FileStore(folders, max_file_size=64, chunk_size=4)
    new_root, = replacement.roots(directory=directory)
    with replacement.open_download(new_root, info.relative_path) as download:
        assert b"".join(download.chunks()) == b"abcdefghij"


def test_upload_is_invisible_until_verified_commit_and_close_only_aborts_partials(files):
    store, _ = files
    root, = store.roots(directory="input")
    with store.begin_upload(root, "nested/new.png", size=4) as upload:
        upload.write(b"abcd")
        assert not (root.path / "nested/new.png").exists()
        assert len(parts(root.path)) == 1
        info = upload.commit(hashlib.sha256(b"abcd").hexdigest())
    pending = store.begin_upload(root, "unfinished.png", size=4)
    pending.write(b"ab")
    store.close()
    assert (root.path / info.relative_path).read_bytes() == b"abcd"
    assert not parts(root.path) and not (root.path / "unfinished.png").exists()


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), OSError("transfer stopped")])
def test_interrupted_chunk_source_cleans_only_its_partial(files, failure):
    store, _ = files
    root, = store.roots(directory="input")
    kept = put(store, root, "kept.png", b"keep")

    def interrupted():
        yield b"ab"
        raise failure

    with pytest.raises(type(failure)):
        store.upload(root, "interrupted.png", interrupted(), size=4, sha256="unused")
    assert not parts(root.path) and not (root.path / "interrupted.png").exists()
    assert (root.path / kept.relative_path).read_bytes() == b"keep"


@pytest.mark.parametrize(("chunks", "size", "digest"), [
    ([b"abcd"], 4, "0" * 64),
    ([b"ab"], 4, hashlib.sha256(b"ab").hexdigest()),
    ([b"abcd"], 3, hashlib.sha256(b"abcd").hexdigest()),
    ([b"abcde"], 5, hashlib.sha256(b"abcde").hexdigest()),
])
def test_invalid_hash_or_size_never_publishes(files, chunks, size, digest):
    store, _ = files
    root, = store.roots(directory="input")
    with pytest.raises(ValueError):
        store.upload(root, "bad.png", chunks, size=size, sha256=digest)
    assert not (root.path / "bad.png").exists() and not parts(root.path)


def test_size_policy_empty_file_and_typed_buffer_bytes(files):
    store, _ = files
    root, = store.roots(directory="input")
    with pytest.raises(ValueError, match="limit"):
        store.begin_upload(root, "large.bin", size=65)
    info = put(store, root, "empty.bin", b"")
    assert info.size == 0 and (root.path / "empty.bin").read_bytes() == b""
    typed = memoryview(bytearray(b"abcd")).cast("H")
    info = store.upload(root, "typed.bin", [typed], size=4,
                        sha256=hashlib.sha256(b"abcd").hexdigest())
    assert info.size == 4 and (root.path / "typed.bin").read_bytes() == b"abcd"


@pytest.mark.parametrize("path", [
    "../outside", "a/../../outside", "/absolute", "C:/drive", "a\\b", "a//b", ".", "",
    "a/./b", "bad\0name", "name:stream", "CON.txt", "NUL/child", "trailing. ",
])
def test_path_traversal_and_nonportable_aliases_are_rejected(files, path):
    store, _ = files
    root, = store.roots(directory="input")
    with pytest.raises(ValueError):
        store.begin_upload(root, path, size=0)
    assert not root.path.exists()


def test_existing_symlinks_cannot_escape_root(files, tmp_path):
    store, _ = files
    root, = store.roots(directory="input")
    root.path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.bin"
    protected.write_bytes(b"keep")
    try:
        (root.path / "link").symlink_to(outside, target_is_directory=True)
        (root.path / "file-link").symlink_to(protected)
    except OSError:
        pytest.skip("This environment cannot create test symlinks")
    with pytest.raises(ValueError, match="symlink"):
        put(store, root, "link/other.bin", b"new")
    with pytest.raises(ValueError, match="symlink"):
        store.open_download(root, "file-link")
    with pytest.raises(ValueError, match="symlink"):
        store.remove(root, "file-link")
    assert protected.read_bytes() == b"keep" and not (outside / "other.bin").exists()


def test_windows_reparse_bit_is_rejected_without_needing_windows(files, monkeypatch):
    store, _ = files
    root, = store.roots(directory="input")
    root.path.mkdir()
    reparse = root.path / "junction"
    reparse.mkdir()
    original = Path.lstat

    def lstat(path):
        value = original(path)
        if path == reparse:
            return SimpleNamespace(st_mode=value.st_mode,
                                   st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="reparse"):
        put(store, root, "junction/blocked.bin", b"new")
    assert not (reparse / "blocked.bin").exists()


def test_competing_uploads_publish_exactly_one_file(files):
    store, _ = files
    root, = store.roots(directory="input")
    left = store.begin_upload(root, "same.bin", size=4)
    right = store.begin_upload(root, "same.bin", size=4)
    left.write(b"left")
    right.write(b"rght")

    def finish(upload, data):
        try:
            return upload.commit(hashlib.sha256(data).hexdigest())
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(finish, left, b"left")
        b = pool.submit(finish, right, b"rght")
        results = [a.result(), b.result()]
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result is not None)
    assert hashlib.sha256((root.path / "same.bin").read_bytes()).hexdigest() == winner.sha256
    assert not parts(root.path)


def test_atomic_link_does_not_overwrite_late_external_winner(files, monkeypatch):
    store, _ = files
    root, = store.roots(directory="input")
    original = os.link

    def late_winner(source, destination):
        destination.write_bytes(b"existing")
        return original(source, destination)

    monkeypatch.setattr(os, "link", late_winner)
    with pytest.raises(FileExistsError):
        put(store, root, "same.bin", b"new")
    assert (root.path / "same.bin").read_bytes() == b"existing" and not parts(root.path)


def test_no_hardlink_support_fails_explicitly_without_replace_fallback(files, monkeypatch):
    store, _ = files
    root, = store.roots(directory="input")

    def unsupported(*_):
        raise OSError(errno.EOPNOTSUPP, "filesystem cannot link")

    monkeypatch.setattr(os, "link", unsupported)
    with pytest.raises(UnsupportedFileSystem, match="hard links"):
        put(store, root, "new.bin", b"new")
    assert not (root.path / "new.bin").exists() and not parts(root.path)


def test_model_roots_are_explicit_and_cross_root_names_cannot_shadow(files):
    store, folders = files
    a, b = store.roots(category="checkpoints")
    info = put(store, b, "unique.safetensors", b"model")
    assert info.root == b and info.native_name == "unique.safetensors"
    assert folders.get_full_path("checkpoints", info.native_name) == str(b.path / info.relative_path)
    assert "checkpoints" not in folders.filename_list_cache
    with pytest.raises(FileExistsError):
        put(store, a, info.relative_path, b"other")
    assert (b.path / info.relative_path).read_bytes() == b"model"
    assert not (a.path / info.relative_path).exists()
    with pytest.raises(ValueError, match="explicit root"):
        store.begin_upload("checkpoints", "ambiguous.bin", size=0)


def test_model_shadow_created_mid_upload_prevents_commit(files):
    store, _ = files
    a, b = store.roots(category="checkpoints")
    upload = store.begin_upload(b, "same.safetensors", size=3)
    upload.write(b"new")
    a.path.mkdir()
    (a.path / "same.safetensors").write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        upload.commit(hashlib.sha256(b"new").hexdigest())
    assert not (b.path / "same.safetensors").exists() and not parts(b.path)
    assert (a.path / "same.safetensors").read_bytes() == b"existing"


def test_native_lookup_mismatch_rolls_back_only_the_new_file(files, monkeypatch):
    store, folders = files
    _, b = store.roots(category="checkpoints")
    monkeypatch.setattr(folders, "get_full_path", lambda *_: "/other/root/model.safetensors")
    with pytest.raises(FileChangedError, match="Native model lookup"):
        put(store, b, "model.safetensors", b"new")
    assert not (b.path / "model.safetensors").exists() and not parts(b.path)


def test_root_config_change_aborts_without_publishing_to_old_or_new_root(files, tmp_path):
    store, folders = files
    root, = store.roots(directory="input")
    upload = store.begin_upload(root, "new.png", size=3)
    upload.write(b"new")
    folders.input = tmp_path / "changed-input"
    with pytest.raises(ValueError, match="configuration changed"):
        upload.commit(hashlib.sha256(b"new").hexdigest())
    assert not (root.path / "new.png").exists() and not parts(root.path)
    assert not folders.input.exists()


def test_remove_requires_one_regular_file_and_respects_revision(files):
    store, _ = files
    root, = store.roots(directory="input")
    info = put(store, root, "kept.bin", b"keep")
    (root.path / "folder").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        store.remove(root, "folder")
    (root.path / info.relative_path).write_bytes(b"changed")
    with pytest.raises(FileChangedError):
        store.remove(root, info.relative_path, revision=info.revision)
    assert (root.path / info.relative_path).read_bytes() == b"changed"
    store.remove(root, info.relative_path)
    with pytest.raises(FileNotFoundError):
        store.remove(root, info.relative_path)
    store.remove(root, info.relative_path, missing_ok=True)
    assert (root.path / "folder").is_dir()


@pytest.mark.parametrize("change", ["rewrite", "remove"])
def test_download_rejects_source_changes_and_closes_stream(files, change):
    store, _ = files
    root, = store.roots(directory="input")
    info = put(store, root)
    download = store.open_download(root, info.relative_path)
    chunks = download.chunks()
    assert next(chunks) == b"abcd"
    target = root.path / info.relative_path
    if change == "rewrite":
        target.write_bytes(b"completely changed")
    else:
        try:
            target.unlink()
        except PermissionError:
            download.close()
            pytest.skip("This platform denies unlinking an open file")
    with pytest.raises(FileChangedError):
        list(chunks)
    assert download._stream.closed


def test_unknown_download_hash_and_abandoned_stream_are_bounded(files):
    store, _ = files
    root, = store.roots(directory="output")
    root.path.mkdir()
    (root.path / "native.png").write_bytes(b"native-output")
    with store.open_download(root, "native.png") as download:
        assert download.metadata.sha256 == hashlib.sha256(b"native-output").hexdigest()
        iterator = download.chunks()
        assert len(next(iterator)) <= store.chunk_size
        iterator.close()
        assert download._stream.closed
    (root.path / "too-large.bin").write_bytes(b"x" * 65)
    with pytest.raises(ValueError, match="limit"):
        store.open_download(root, "too-large.bin")


@pytest.mark.parametrize("name", [
    "literal [output]", "pixel[output]", "pixel[input]", "pixel[temp]",
    "[output]", "[input]", "[temp]", "nested/pixel[output]",
])
def test_input_annotation_suffix_does_not_select_another_directory(files, name):
    store, _ = files
    root, = store.roots(directory="input")
    info = put(store, root, name, b"data")
    assert info.relative_path == name
    assert info.native_name == name + " [input]"
    assert (root.path / name).read_bytes() == b"data"


def test_flush_and_close_errors_still_cleanup_the_failed_upload(files):
    store, _ = files
    root, = store.roots(directory="input")
    upload = store.begin_upload(root, "failed.bin", size=3)
    upload.write(b"new")
    actual = upload._stream

    class FailingStream:
        def flush(self):
            raise OSError("simulated disk-full flush")

        def close(self):
            actual.close()
            raise OSError("simulated close failure")

    upload._stream = FailingStream()
    with pytest.raises(OSError):
        upload.commit(hashlib.sha256(b"new").hexdigest())
    assert upload._closed and not store._uploads
    assert not parts(root.path) and not (root.path / "failed.bin").exists()


def test_download_growing_during_prehash_is_rejected_and_closed(files, monkeypatch):
    store, _ = files
    root, = store.roots(directory="output")
    root.path.mkdir()
    target = root.path / "changing.bin"
    target.write_bytes(b"first")
    original = os.fdopen
    opened = []

    class GrowingStream:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size):
            chunk = self.stream.read(size)
            if not self.changed:
                with target.open("ab") as writer:
                    writer.write(b"appended")
                self.changed = True
            return chunk

    def growing(*args):
        stream = GrowingStream(original(*args))
        opened.append(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", growing)
    with pytest.raises(FileChangedError):
        store.open_download(root, "changing.bin")
    assert opened[0].closed


def test_prehashing_one_download_does_not_block_an_unrelated_upload(files, monkeypatch):
    store, _ = files
    source, = store.roots(directory="output")
    destination, = store.roots(directory="input")
    put(store, source)
    entered, release = threading.Event(), threading.Event()
    original = os.fdopen

    class PausedStream:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release the hash reader")
            return self.stream.read(size)

    monkeypatch.setattr(os, "fdopen", lambda *args: PausedStream(original(*args)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        hashing = pool.submit(store.open_download, source, "photo.png")
        try:
            assert entered.wait(2)
            uploading = pool.submit(put, store, destination, "other.bin", b"new")
            assert uploading.result(timeout=2).size == 3
        finally:
            release.set()
        hashing.result(timeout=2).close()
