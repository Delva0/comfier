"""Saved file descriptions are reauthorized and hashed by the current daemon."""

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from comani import Client, FileDescriptor, RemoteError
from comani.daemon.files import FileStore
from comani.daemon.management_http import create_management_app
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor

from .test_binary_fetch import serve
from .test_files_api import running
from .test_runtime_files import Folders


class FileSupervisor(RuntimeSupervisor):
    """Real management HTTP and FileStore without starting native computation."""

    def __init__(self, base):
        super().__init__(RuntimeConfig(str(base / "uninstalled"), workspace=str(base)))
        self.file_store = FileStore(Folders(base / "native"), max_file_size=None)

    async def get_file_store(self):
        return self.file_store


async def test_management_reconstruction_reauthorizes_and_hashes_saved_descriptor(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"first bytes")
    first = FileSupervisor(tmp_path)
    async with serve(create_management_app(first)) as url:
        async with Client(url) as remote:
            original = await remote.files.upload(source, directory="output", path="shots/front.bin")
            saved = json.loads(json.dumps(asdict(original.describe())))
            assert set(saved) == {"version", "directory", "category", "root_path",
                                  "relative_path", "size", "sha256"}
            assert (await remote.files.stat(original)).sha256 is None
            with pytest.raises(ValueError, match="checksum=True"):
                (await remote.files.stat(original)).describe()
            assert first.process is None

    second = FileSupervisor(tmp_path)
    async with serve(create_management_app(second)) as url:
        async with Client(url) as remote:
            with pytest.raises(RemoteError):
                await remote.files.stat(original, checksum=True)
            restored = await remote.files.resolve(saved)
            assert restored.root.id != original.root.id
            assert restored.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
            assert restored.relative_path == original.relative_path
            assert (await remote.files.resolve(FileDescriptor(**saved))) == restored
            downloaded = await remote.files.download(restored, tmp_path / "restored.bin")
            assert downloaded.local_path.read_bytes() == source.read_bytes()
            assert second.process is None

            # Same length must not bypass the required full-content comparison.
            target = Path(restored.root.path) / restored.relative_path
            target.write_bytes(b"other bytes")
            with pytest.raises(RemoteError, match="SHA256"):
                await remote.files.resolve(saved)
            # The server forces hashing even if a raw caller asks to skip it.
            with pytest.raises(RemoteError, match="SHA256"):
                await remote.transport.request("file_stat", session_id=remote.session_id,
                                               checksum=False, descriptor=saved)
            target.unlink()
            with pytest.raises(RemoteError):
                await remote.files.resolve(saved)


async def test_descriptor_never_searches_another_root_or_trusts_external_root_path(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"same bytes")
    async with running(tmp_path / "first") as (_, url):
        async with Client(url, token="file-token") as remote:
            original = await remote.files.upload(source, directory="output", path="same.bin")
            saved = original.describe()
            input_file = await remote.files.upload(source, directory="input", path="same.bin")
            # Same path and identical bytes in a different configured root are insufficient.
            with pytest.raises(RemoteError, match="current native configuration"):
                await remote.files.resolve(replace(saved, directory="input"))
            with pytest.raises(RemoteError):
                await remote.files.resolve(replace(saved, relative_path="../input/same.bin"))
            with pytest.raises(RemoteError, match="current native configuration"):
                await remote.files.resolve(replace(saved, root_path=str(tmp_path)))
            await remote.files.remove(original)
            with pytest.raises(RemoteError):
                await remote.files.resolve(saved)
            assert await remote.files.stat(input_file, checksum=True) == input_file

    async with running(tmp_path / "other-daemon") as (_, url):
        async with Client(url, token="file-token") as remote:
            await remote.files.upload(source, directory="output", path="same.bin")
            with pytest.raises(RemoteError, match="current native configuration"):
                await remote.files.resolve(saved)


async def test_model_descriptor_uses_exact_current_category_root(tmp_path):
    source = tmp_path / "model.bin"
    source.write_bytes(b"model bytes")
    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            _, root = await remote.files.roots(category="checkpoints")
            model = await remote.files.upload(source, root=root, path="model.bin")
            saved = model.describe()
            assert saved.directory is None and saved.category == "checkpoints"
            assert await remote.files.resolve(saved) == model
            runtime._files.folder_paths.models["checkpoints"] = []
            with pytest.raises(RemoteError, match="current native configuration"):
                await remote.files.resolve(saved)


@pytest.mark.parametrize("fields", [
    {"version": 2}, {"version": True}, {"directory": "elsewhere"},
    {"category": "checkpoints"}, {"size": -1}, {"size": True},
    {"sha256": None}, {"sha256": ""}, {"sha256": "not-a-checksum"},
])
def test_invalid_descriptors_do_not_drop_required_identity(fields):
    value = {"root_path": "/native/output", "directory": "output",
             "relative_path": "front.bin", "size": 4, "sha256": "0" * 64}
    with pytest.raises((TypeError, ValueError)):
        FileDescriptor(**(value | fields))
