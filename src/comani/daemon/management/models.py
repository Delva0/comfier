"""Provider-to-daemon acquisition, verified before native create-only publish."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import aiohttp

from comani.files import _io

from ..file_http import metadata_wire
from ..files import Revision, _relative, verify_staged
from .operations import ManagementError
from .providers import provider_headers
from .tools import python_metadata
from .transports import external_download, select_transport


def verify_git_blob(path, expected):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected):
        raise ManagementError("InvalidArgument", "HF Git blob hash must be a 40-character object ID")
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected.lower():
        raise ManagementError("ChecksumMismatch", "HF Git blob digest differs from the resolved revision")


class ModelService:
    def __init__(self, supervisor, resolver):
        self.supervisor, self.resolver = supervisor, resolver
        self._targets = set()

    async def categories(self):
        store = await self.supervisor.get_file_store()
        categories = sorted(set(store.folder_paths.folder_names_and_paths) - {"custom_nodes", "datasets"})
        return {"categories": categories, "generation": self.supervisor.roots_generation,
                "observed": "live" if self.supervisor.state == "running" else "last-observed"}

    async def listing(self, *, category=None, root=None, prefix="", limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ManagementError("InvalidArgument", "Model listing limit must be between 1 and 10000")
        store = await self.supervisor.get_file_store()
        category = category or (root.get("category") if isinstance(root, dict) else None)
        prefix = prefix or ""
        root_id = root.get("id") if isinstance(root, dict) else root
        if category is None and root_id:
            category = store.root_by_id(root_id).category
            if category is None:
                raise ManagementError("InvalidArgument", "Select a native model category root")
        roots = store.roots(category=category) if category else tuple(
            selected for name in sorted(store.folder_paths.folder_names_and_paths)
            if name not in {"custom_nodes", "datasets"} for selected in store.roots(category=name))
        if root_id:
            roots = tuple(value for value in roots if value.id == root_id)
            if not roots:
                raise ManagementError("InvalidArgument", "Root is not in the selected native category")
        result = []
        for selected in roots:
            if not selected.path.exists():
                continue
            for parent, dirs, names in os.walk(selected.path, followlinks=False):
                dirs[:] = sorted(name for name in dirs if not name.startswith(".") and not (Path(parent) / name).is_symlink())
                for name in sorted(names):
                    path = Path(parent) / name
                    relative = path.relative_to(selected.path).as_posix()
                    if name.startswith(".") or not relative.startswith(prefix) or path.is_symlink():
                        continue
                    try:
                        with store.open_download(selected, relative, checksum=False) as source:
                            result.append(metadata_wire(store, source.metadata))
                    except (OSError, ValueError):
                        continue
                    if len(result) >= limit:
                        return {"files": result, "truncated": True}
        return {"files": result, "truncated": False}

    async def _fetch(self, context, artifact, target, *, max_bytes, timeout):
        expected_size = artifact.get("size")
        expected_hash = artifact.get("sha256")
        if expected_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            raise ManagementError("InvalidArgument", "Expected SHA256 must contain 64 hexadecimal characters")
        # Civitai reports rounded KiB, not an authoritative Content-Length. A
        # provider SHA is authoritative; response length is checked separately.
        if artifact.get("provider") == "civitai":
            expected_size = None
        if expected_size is not None and (type(expected_size) is not int or expected_size < 0 or (max_bytes is not None and expected_size > max_bytes)):
            raise ManagementError("InvalidArgument", "Artifact exceeds the configured file limit")
        self._space(target.parent, expected_size)
        validator, known_total = None, expected_size
        for attempt in range(4):
            offset = target.stat().st_size
            address = await self.resolver.address(artifact)
            headers = {**provider_headers(address), "Accept-Encoding": "identity"}
            if offset and not (validator or expected_hash or artifact.get("revision")):
                target.write_bytes(b"")
                offset = 0
                context.log("Source has no stable validator; restarting from byte zero\n")
            if offset:
                headers["Range"] = f"bytes={offset}-"
                if validator:
                    headers["If-Range"] = validator
            context.progress(phase="downloading", bytes=offset, total=known_total, attempt=attempt + 1)
            try:
                client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=timeout or 60)
                async with aiohttp.ClientSession(timeout=client_timeout, auto_decompress=False) as session:
                    async with session.get(address, headers=headers) as response:
                        current_validator = response.headers.get("ETag")
                        if current_validator and current_validator.startswith("W/"):
                            current_validator = None
                        current_validator = current_validator or response.headers.get("Last-Modified")
                        if response.status == 416:
                            match = re.fullmatch(r"bytes \*/(\d+)", response.headers.get("Content-Range", ""))
                            if (expected_hash and match and offset == int(match[1])
                                    and (known_total is None or offset == known_total)):
                                stage = await _io(verify_staged, target, size=offset)
                                if stage.sha256 == expected_hash.lower():
                                    return stage, "sha256"
                            target.write_bytes(b"")
                            validator, known_total = None, expected_size
                            context.log("Range was not satisfiable; restarting and revalidating\n")
                            continue
                        if response.status not in {200, 206}:
                            raise ManagementError("ProviderError", f"Download returned HTTP {response.status}")
                        if "text/html" in response.headers.get("Content-Type", "").lower():
                            raise ManagementError("ProviderError", "Provider returned an HTML page instead of model bytes")
                        if response.headers.get("Content-Encoding", "identity") != "identity":
                            raise ManagementError("ProviderError", "Encoded model response cannot be safely resumed")
                        response_end = None
                        if response.status == 206:
                            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                            if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[2]) >= int(match[3]):
                                raise ManagementError("InvalidRangeResponse", "Provider returned an inconsistent Content-Range")
                            total = int(match[3])
                            response_end = int(match[2]) + 1
                            if known_total is not None and total != known_total:
                                raise ManagementError("ArtifactChanged", "Provider length changed during acquisition")
                            if offset and validator and current_validator != validator:
                                target.write_bytes(b"")
                                validator, known_total = None, expected_size
                                context.log("Source validator changed; restarting from byte zero\n")
                                continue
                            known_total = total
                        else:
                            if offset:
                                context.log("Provider ignored Range; safely restarting from byte zero\n")
                            offset = 0
                            target.write_bytes(b"")
                            total = response.content_length
                            if expected_size is not None and total is not None and expected_size != total:
                                raise ManagementError("ArtifactChanged", "Provider length differs from the resolved artifact")
                            known_total = total if total is not None else expected_size
                        validator = current_validator
                        if known_total is not None and max_bytes is not None and known_total > max_bytes:
                            raise ManagementError("FileTooLarge", "Download exceeds the configured file limit")
                        self._space(target.parent, None if known_total is None else known_total - offset)
                        received = offset
                        with target.open("ab") as stream:
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                received += len(chunk)
                                if response_end is not None and received > response_end:
                                    raise ManagementError("InvalidRangeResponse", "Response body exceeded its declared Content-Range")
                                if (max_bytes is not None and received > max_bytes) or (known_total is not None and received > known_total):
                                    raise ManagementError("FileTooLarge", "Download exceeded its declared length or configured limit")
                                await _io(stream.write, chunk)
                                context.progress(phase="downloading", bytes=received, total=known_total)
                        if response_end is not None and received != response_end:
                            raise aiohttp.ClientPayloadError("Incomplete range response")
                        if known_total is not None and received != known_total:
                            raise aiohttp.ClientPayloadError("Incomplete response")
                        stage = await _io(verify_staged, target, size=received)
                        if expected_hash and stage.sha256 != expected_hash.lower():
                            raise ManagementError("ChecksumMismatch", "Model SHA256 differs from the provider digest")
                        return stage, "sha256" if expected_hash else None
            except (aiohttp.ClientError, TimeoutError):
                if attempt == 3:
                    raise ManagementError("ProviderDisconnected", "Provider transfer failed after bounded retries") from None
                context.log("Provider connection interrupted; retrying the same artifact\n")
                await asyncio.sleep(min(0.25 * 2 ** attempt, 2))
        raise ManagementError("ProviderDisconnected", "Provider could not provide a verifiable complete file")

    async def download(self, context, *, artifact, root, path=None, reuse=False, timeout=None,
                       transport="auto", connections=8, _max_bytes=None):
        artifact = await self.resolver.prepare(artifact)
        if type(reuse) is not bool:
            raise ManagementError("InvalidArgument", "reuse must be a boolean")
        path = path or artifact.get("filename")
        _relative(path)
        store = await self.supervisor.get_file_store()
        root_id = root.get("id") if isinstance(root, dict) else root
        selected = store.root_by_id(root_id)
        if selected.category is None:
            raise ManagementError("InvalidArgument", "Select a native model category root")
        generation = self.supervisor.roots_generation
        key = selected.category, path
        if any(category == key[0] and (name == path or name.startswith(path + "/") or path.startswith(name + "/"))
               for category, name in self._targets):
            raise ManagementError("DestinationBusy", "Another operation is publishing this model path")
        self.resolver.retain(artifact)
        self._targets.add(key)
        partial = staging_directory = root_identity = None
        try:
            # Fail before provider traffic for conflicting destinations.
            try:
                store._conflicts(selected, path)
            except FileExistsError:
                if not reuse or not artifact.get("sha256"):
                    raise ManagementError("FileExists", "Destination exists; verified SHA256 reuse must be explicit") from None
                with await asyncio.to_thread(store.open_download, selected, path) as existing:
                    if existing.metadata.sha256 != artifact["sha256"].lower():
                        raise ManagementError("ChecksumMismatch", "Existing file differs from the selected artifact")
                    return {"file": metadata_wire(store, existing.metadata), "artifact": self.resolver.public(artifact),
                            "sha256": existing.metadata.sha256, "verified_against": "sha256", "reused": True,
                            "native_visibility": "visible" if self.supervisor.state == "running" else "unknown"}
            root_identity = await _io(store.pin_root, selected, _cancel_cleanup=lambda value: value.close())
            staging = Path(self.supervisor.config.workspace) / ".comani" / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            staging_directory = Path(tempfile.mkdtemp(prefix="model-", dir=staging))
            partial = staging_directory / "payload.part"
            partial.touch(mode=0o600, exist_ok=False)
            max_bytes = store.max_file_size
            if _max_bytes is not None:
                max_bytes = _max_bytes if max_bytes is None else min(max_bytes, _max_bytes)
            stage, verified, selected_transport = await self._acquire(
                context, artifact, partial, max_bytes, transport, connections, timeout)
            size, actual = stage.size, stage.sha256
            context.progress(phase="verifying", bytes=size, total=size)
            await self._revalidate_root(store, root_identity, generation)
            context.progress(phase="publishing", bytes=size, total=size)
            info = await self._publish_staged(store, selected, path, stage,
                                              check_root=lambda: self._check_root(root_identity))
            result = {"file": metadata_wire(store, info), "artifact": self.resolver.public(artifact), "sha256": actual,
                      "verified_against": verified, "reused": False,
                      "native_visibility": "visible" if self.supervisor.state == "running" else "unknown",
                      "transport": selected_transport}
            context.partial(result)
            if hasattr(self.supervisor, "files_changed"):
                try:
                    await self.supervisor.files_changed(selected.category)
                except (Exception, asyncio.CancelledError):
                    # Publication already committed. A cancelled cache-refresh
                    # observer cannot turn that into a cancelled installation.
                    result["native_visibility"] = "unknown"
            context.progress(phase="complete", bytes=size, total=size)
            return result
        finally:
            self._targets.discard(key)
            self.resolver.release(artifact)
            if root_identity is not None:
                root_identity.close()
            if staging_directory is not None:
                await _io(shutil.rmtree, staging_directory)

    async def _revalidate_root(self, store, identity, generation):
        current = await self.supervisor.get_file_store()
        try:
            if current is not store:
                raise ValueError("Native file store changed")
            # Keep the previous conservative policy where a directory inode
            # cannot be held open against replacement/reuse (e.g. Windows).
            if identity.fd is None and generation != self.supervisor.roots_generation:
                raise ValueError("Native roots changed without a directory identity handle")
            await _io(self._check_root, identity)
        except (ValueError, OSError):
            raise ManagementError("RootsChanged", "Selected native root changed during acquisition; select the destination again") from None

    @staticmethod
    def _check_root(identity):
        try:
            identity.check()
        except (ValueError, OSError):
            raise ManagementError("RootsChanged", "Selected native root changed during acquisition; select the destination again") from None

    @staticmethod
    async def _publish_staged(store, root, path, stage, *, check_root=None):
        # Cross-filesystem copying remains cancellable between chunks. Always
        # drain the worker; if atomic publication already won, record its result
        # rather than report a cancelled operation with an unrecorded file.
        stop, completed = threading.Event(), {}

        def remember(info):
            completed["info"] = info

        worker = asyncio.create_task(_io(store.publish_staged, root, path, stage, cancel=stop,
                                         check_root=check_root, _cancel_cleanup=remember))
        cancelled = False
        while True:
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
                cancelled = True
                if worker.done():
                    if "info" in completed:
                        return completed["info"]
                    raise
            except InterruptedError:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise

    async def _acquire(self, context, artifact, target, max_bytes, transport, connections, timeout):
        hf_available = False
        if artifact.get("provider") == "huggingface" and transport in {"auto", "hf"}:
            if not hasattr(context, "provider_environment"):
                context.provider_environment = await python_metadata(self.supervisor.config.runtime_python)
            hf_available = "huggingface-hub" in context.provider_environment["packages"]
            if transport == "hf" and not hf_available:
                raise ManagementError("MissingDownloadTool", "Install an explicit huggingface-hub version with comfyui.prepare_tools before selecting hf")
        selected = select_transport(artifact, transport, hf_available=hf_available)
        context.progress(phase="downloading", transport=selected)
        if selected == "http":
            stage, verified = await self._fetch(context, artifact, target, max_bytes=max_bytes, timeout=timeout)
            if artifact.get("git_sha1"):
                await _io(verify_git_blob, target, artifact["git_sha1"])
                verified = verified or "git-sha1"
            stage.check()
            return stage, verified, selected
        expected = artifact.get("sha256")
        if expected is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise ManagementError("InvalidArgument", "Expected SHA256 must contain 64 hexadecimal characters")
        if type(artifact.get("size")) is int and max_bytes is not None and artifact["size"] > max_bytes:
            raise ManagementError("FileTooLarge", "Artifact exceeds the configured file limit")
        self._space(target.parent, artifact.get("size"))
        try:
            await self._external(context, artifact, target, selected, connections, timeout, max_bytes)
        except ManagementError as exc:
            if exc.code != "UnsafeDownloadTransport" and (
                transport != "auto" or exc.code not in {"ProviderTransferFailed", "MissingDownloadTool"}
            ):
                raise
            context.log("Optional engine could not acquire this artifact; falling back to validated HTTP\n")
            context.progress(transport="http", transport_fallback={"from": selected,
                "reason": exc.code, "exit_code": exc.details.get("exit_code"),
                "diagnostic": exc.details.get("diagnostic", "")})
            target.write_bytes(b"")
            stage, verified = await self._fetch(context, artifact, target, max_bytes=max_bytes, timeout=timeout)
            if artifact.get("git_sha1"):
                await _io(verify_git_blob, target, artifact["git_sha1"])
                verified = verified or "git-sha1"
            stage.check()
            return stage, verified, "http"
        stage = await _io(verify_staged, target)
        size, actual = stage.size, stage.sha256
        if (max_bytes is not None and size > max_bytes) or (artifact.get("provider") != "civitai" and artifact.get("size") is not None and size != artifact["size"]):
            raise ManagementError("ArtifactChanged", "Downloaded bytes differ from the resolved length",
                                  {"actual_size": size, "expected_size": artifact.get("size")})
        if expected and actual != expected.lower():
            raise ManagementError("ChecksumMismatch", "Model SHA256 differs from the provider digest")
        verified = "sha256" if expected else None
        if artifact.get("git_sha1"):
            await _io(verify_git_blob, target, artifact["git_sha1"])
            verified = verified or "git-sha1"
        stage.check()
        return stage, verified, selected

    async def snapshot(self, context, *, manifest, root, path, publish, transport="auto",
                       connections=8, timeout=None, max_total_bytes=None):
        """A fixed manifest has explicit directory or per-file publication."""
        if publish not in {"directory", "files"}:
            raise ManagementError("InvalidArgument", "Select publish='directory' or publish='files' explicitly")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list) or not manifest["files"]:
            raise ManagementError("InvalidArgument", "Expected a nonempty explicit model manifest")
        if len(manifest["files"]) > 10000:
            raise ManagementError("InvalidArgument", "Manifest exceeds 10000 files")
        if max_total_bytes is not None and (type(max_total_bytes) is not int or max_total_bytes < 0):
            raise ManagementError("InvalidArgument", "max_total_bytes must be None or a nonnegative integer")
        _relative(path)
        entries, seen = [], set()
        for artifact in manifest["files"]:
            relative = artifact.get("relative_path") or (artifact.get("source") or {}).get("path") or artifact.get("filename")
            _relative(relative)
            if relative in seen:
                raise ManagementError("InvalidArgument", "Manifest contains duplicate destination paths")
            if artifact.get("provider") == "huggingface" and artifact.get("revision") != manifest.get("revision"):
                raise ManagementError("InvalidArgument", "All HF files must belong to the manifest's fixed revision")
            seen.add(relative)
            entries.append((relative, self.resolver.bind(artifact)))
        known_total = sum(item.get("size", 0) or 0 for _, item in entries)
        if max_total_bytes is not None and known_total > max_total_bytes:
            raise ManagementError("FileTooLarge", "Manifest exceeds the explicit total staging quota")
        results = []
        result = {"publish": publish, "revision": manifest.get("revision"), "files": results,
                  "complete": False, "partial_changes": False}
        context.partial(result)
        if publish == "files":
            completed_bytes = 0
            try:
                for index, (relative, artifact) in enumerate(entries):
                    context.progress(file=index + 1, files=len(entries), path=relative)
                    item = await self.download(context, artifact=artifact, root=root,
                        path=path + "/" + relative, transport=transport, connections=connections, timeout=timeout,
                        _max_bytes=None if max_total_bytes is None else max_total_bytes - completed_bytes)
                    results.append(item)
                    completed_bytes += item["file"]["size"]
                    result["partial_changes"] = True
                    context.partial(result)
            except BaseException:
                context.partial(result)
                raise
            result["complete"] = True
            return result
        store = await self.supervisor.get_file_store()
        selected = store.root_by_id(root.get("id") if isinstance(root, dict) else root)
        if selected.category is None:
            raise ManagementError("InvalidArgument", "Select a native model category root")
        generation = self.supervisor.roots_generation
        key = selected.category, path
        if any(category == key[0] and (name == path or name.startswith(path + "/") or path.startswith(name + "/"))
               for category, name in self._targets):
            raise ManagementError("DestinationBusy", "Another operation is publishing inside this snapshot destination")
        self._targets.add(key)
        staging = root_identity = None
        try:
            store._conflicts(selected, path)
            root_identity = await _io(store.pin_root, selected, _cancel_cleanup=lambda value: value.close())
            parent = store._parent(selected, path, create=True)
            identity = (parent.stat().st_dev, parent.stat().st_ino)
            # Stage outside every native root on the same filesystem, so native
            # recursive filename scans cannot observe incomplete tree contents.
            native_roots = [Path(value).resolve() for paths, _ in store.folder_paths.folder_names_and_paths.values() for value in paths]
            staging_parent = parent
            while any(staging_parent == value or value in staging_parent.parents for value in native_roots):
                if staging_parent.parent == staging_parent:
                    raise ManagementError("UnsupportedFileSystem", "No staging directory exists outside native roots")
                staging_parent = staging_parent.parent
            if staging_parent.stat().st_dev != parent.stat().st_dev:
                raise ManagementError("UnsupportedFileSystem", "Atomic snapshot staging requires the destination filesystem")
            staging = Path(tempfile.mkdtemp(prefix=".comani-snapshot-", dir=staging_parent))
            tree = staging / "tree"
            tree.mkdir()
            self._space(staging, known_total)
            total_bytes = 0
            for index, (relative, artifact) in enumerate(entries):
                context.progress(file=index + 1, files=len(entries), path=relative)
                transfer_directory = Path(tempfile.mkdtemp(prefix="acquire-", dir=staging))
                temporary = transfer_directory / "payload.part"
                temporary.touch(mode=0o600, exist_ok=False)
                self.resolver.retain(artifact)
                try:
                    max_bytes = store.max_file_size
                    if max_total_bytes is not None:
                        remaining = max_total_bytes - total_bytes
                        max_bytes = remaining if max_bytes is None else min(max_bytes, remaining)
                    stage, verified, engine = await self._acquire(
                        context, artifact, temporary, max_bytes, transport, connections, timeout)
                finally:
                    self.resolver.release(artifact)
                destination = tree / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage.check()
                temporary.replace(destination)
                await _io(shutil.rmtree, transfer_directory)
                total_bytes += stage.size
                results.append({"relative_path": relative, "sha256": stage.sha256, "size": stage.size,
                                "verified_against": verified, "transport": engine, "artifact": self.resolver.public(artifact)})
                context.partial(result)
            await self._revalidate_root(store, root_identity, generation)
            with store._lock:
                self._check_root(root_identity)
                current = store._parent(selected, path)
                if (current.stat().st_dev, current.stat().st_ino) != identity:
                    raise ManagementError("RootsChanged", "Snapshot parent directory changed")
                store._conflicts(selected, path)
                destination = parent / _relative(path)[-1]
                self._rename_directory(tree, destination)
                store._invalidate(selected)
                result["complete"], result["partial_changes"] = True, True
                result["path"], result["root"] = path, root
                for item in results:
                    relative = path + "/" + item["relative_path"]
                    info = store._metadata(selected, relative, item["sha256"], (selected.path / relative).stat())
                    item["file"] = metadata_wire(store, info)
                context.partial(result)
            if hasattr(self.supervisor, "files_changed"):
                try:
                    await self.supervisor.files_changed(selected.category)
                except (Exception, asyncio.CancelledError):
                    result["native_visibility"] = "unknown"
            return result
        finally:
            self._targets.discard(key)
            if root_identity is not None:
                root_identity.close()
            if staging is not None:
                await _io(shutil.rmtree, staging)

    @staticmethod
    def _rename_directory(source, destination, *, source_fd=None, destination_fd=None):
        if source_fd is not None:
            _relative(os.fspath(source))
        if destination_fd is not None:
            _relative(os.fspath(destination))
        try:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError:
            raise ManagementError("UnsupportedFileSystem", "Atomic create-only directories require Linux renameat2") from None
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100 if source_fd is None else source_fd, os.fsencode(source),
                  -100 if destination_fd is None else destination_fd, os.fsencode(destination), 1) != 0:
            number = ctypes.get_errno()
            if number == errno.EEXIST:
                raise ManagementError("FileExists", "Snapshot destination already exists")
            raise OSError(number, os.strerror(number))

    async def _external(self, context, artifact, target, transport, connections, timeout, max_bytes):
        transfer = asyncio.create_task(external_download(context, self.resolver, artifact, target,
            transport=transport, connections=connections, timeout=timeout,
            python=self.supervisor.config.runtime_python))
        try:
            while not transfer.done():
                # Official Hub cache + final copy can temporarily occupy twice
                # the artifact size. All engine files live in this owned dir.
                total = 0
                for path in target.parent.rglob("*"):
                    try:
                        value = path.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(value.st_mode):
                        total += value.st_size
                if max_bytes is not None and total > max_bytes * 2 + 65536:
                    transfer.cancel()
                    await asyncio.gather(transfer, return_exceptions=True)
                    raise ManagementError("FileTooLarge", "Download staging exceeded its bounded disk allowance")
                done, _ = await asyncio.wait({transfer}, timeout=0.1)
                if done:
                    break
            return await transfer
        except BaseException:
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
            raise

    @staticmethod
    def _space(directory, additional):
        if additional is not None and additional > shutil.disk_usage(directory).free:
            raise ManagementError("DiskFull", "Insufficient available disk space for the resolved artifact")

    async def remove(self, context, *, file, missing_ok=False):
        async with self.supervisor.maintenance_lock:
            self.supervisor.assert_stopped()
            store = await self.supervisor.get_file_store()
            root = store.root_by_id(file["root"]["id"])
            if root.category is None:
                raise ManagementError("InvalidArgument", "Model removal requires a native model category")
            revision = Revision(*file["revision"]) if file.get("revision") else None
            await asyncio.to_thread(store.remove, root, file["relative_path"], revision=revision, missing_ok=missing_ok)
            return {"removed": True, "file": file, "loaded_objects_unloaded": False}
