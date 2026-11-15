"""Internal native-file operations; callers own scheduling and transfer lifetimes.

This layer has no sessions, HTTP or SDK return types. Paths are checked against
the current native configuration. It does not isolate mutually hostile code
running with the daemon's own filesystem permissions.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath


class FileChangedError(OSError):
    code = "FileChanged"


class UnsupportedFileSystem(OSError):
    code = "UnsupportedFileSystem"


@dataclass(frozen=True)
class Root:
    id: str
    path: Path
    directory: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Revision:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, value):
        return cls(value.st_dev, value.st_ino, value.st_size,
                   value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class Metadata:
    root: Root
    relative_path: str
    native_name: str
    size: int
    sha256: str | None
    revision: Revision


def _linked(value):
    # Windows junctions and other reparse points need not be S_IFLNK.
    return (stat.S_ISLNK(value.st_mode)
            or bool(getattr(value, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _regular(path):
    value = path.lstat()
    if _linked(value) or not stat.S_ISREG(value.st_mode):
        raise ValueError("Expected a regular file without symlinks or reparse points")
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or value.startswith("/") or any(char in value for char in '<>:"|?*')
            or any(part in {"", ".", ".."} or part.endswith((".", " "))
                   or PureWindowsPath(part).is_reserved()
                   for part in value.split("/"))):
        raise ValueError("Expected a root-relative path using / without traversal")
    return value.split("/")


def _directory(path, *, create=False):
    """Check canonical roots and descendants without following further links.

    roots() has already resolved any symlinks explicitly in native root config.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            if not create:
                raise
            try:
                current.mkdir()
            except FileExistsError:
                pass
            value = current.lstat()
        if _linked(value) or not stat.S_ISDIR(value.st_mode):
            raise ValueError("File directories cannot contain symlinks or reparse points")
    return path


def _unlink_owned(path, identity):
    try:
        value = _regular(path)
    except FileNotFoundError:
        return
    if (value.st_dev, value.st_ino) != identity:
        raise FileChangedError("Refusing to remove a replaced transfer file")
    path.unlink()


class FileStore:
    """Synchronous, chunk-bounded filesystem primitives for an I/O worker.

    max_file_size=None adds no deployment quota. Declared sizes and chunk bounds
    still constrain each transfer. Completed files have no session owner.
    """

    def __init__(self, folder_paths, *, max_file_size, chunk_size=1024 * 1024):
        if max_file_size is not None and (type(max_file_size) is not int or max_file_size < 0):
            raise ValueError("max_file_size must be None or a nonnegative integer")
        if type(chunk_size) is not int or not 0 < chunk_size <= 8 * 1024 * 1024:
            raise ValueError("chunk_size must be between 1 byte and 8 MiB")
        self.folder_paths = folder_paths
        self.max_file_size = max_file_size
        self.chunk_size = chunk_size
        self._roots = {}
        self._uploads = set()
        self._lock = threading.RLock()

    def roots(self, *, directory=None, category=None):
        if directory is not None and category is not None:
            raise ValueError("Select directory or model category, not both")
        if directory is None and category is None:
            return tuple(self.roots(directory=name)[0] for name in ("input", "temp", "output"))
        if directory is not None:
            if directory not in {"input", "temp", "output"}:
                raise ValueError("Unknown native data directory")
            paths = [getattr(self.folder_paths, f"get_{directory}_directory")()]
        else:
            if not isinstance(category, str) or not category:
                raise ValueError("Expected a native model category")
            mapper = getattr(self.folder_paths, "map_legacy", lambda name: name)
            category = mapper(category)
            if category in {"custom_nodes", "datasets"}:
                raise ValueError("This operation transfers model files, not installed code or datasets")
            paths = self.folder_paths.get_folder_paths(category)
        result = []
        with self._lock:
            for value in paths:
                path = Path(value).expanduser().resolve()
                key = (directory, category, path)
                if key not in self._roots:
                    self._roots[key] = Root(uuid.uuid4().hex, path, directory, category)
                result.append(self._roots[key])
        return tuple(result)

    def _root(self, root):
        if not isinstance(root, Root):
            raise ValueError("Select an explicit root returned by this file store")
        current = self.roots(directory=root.directory, category=root.category)
        if root not in current:
            raise ValueError("The native root configuration changed or belongs to another file store")
        return root

    def root_by_id(self, root_id):
        with self._lock:
            root = next((value for value in self._roots.values() if value.id == root_id), None)
            return self._root(root)

    def _parent(self, root, relative_path, *, create=False):
        parts = _relative(relative_path)
        self._root(root)
        parent = _directory(root.path, create=create)
        for part in parts[:-1]:
            parent = _directory(parent / part, create=create)
        try:
            parent.resolve().relative_to(root.path)
        except ValueError:
            raise ValueError("File path escaped the selected native root") from None
        return parent

    def _conflicts(self, root, relative_path):
        roots = self.roots(category=root.category) if root.category else (root,)
        for candidate in roots:
            try:
                parent = self._parent(candidate, relative_path)
                (parent / _relative(relative_path)[-1]).lstat()
            except FileNotFoundError:
                continue
            raise FileExistsError("File already exists in a selected native category root; use a new name")

    def _invalidate(self, root):
        if root.category:
            cache = getattr(self.folder_paths, "filename_list_cache", None)
            if cache is not None:
                cache.pop(root.category, None)

    def _metadata(self, root, relative_path, digest, value):
        self._root(root)
        name = relative_path
        if root.directory in {"output", "temp"}:
            name += f" [{root.directory}]"
        elif root.directory == "input" and name.endswith(("[input]", "[temp]", "[output]")):
            name += " [input]"
        if root.category:
            actual = self.folder_paths.get_full_path(root.category, relative_path)
            if actual is None or Path(actual).resolve() != root.path / relative_path:
                raise FileChangedError("Native model lookup does not select this file")
        return Metadata(root, relative_path, name, value.st_size, digest, Revision.of(value))

    def begin_upload(self, root, relative_path, *, size):
        if (type(size) is not int or size < 0
                or (self.max_file_size is not None and size > self.max_file_size)):
            raise ValueError("Declared file size exceeds the configured file limit")
        with self._lock:
            self._root(root)
            _relative(relative_path)
            self._conflicts(root, relative_path)
            parent = self._parent(root, relative_path, create=True)
            upload = Upload(self, root, relative_path, parent, size)
            self._uploads.add(upload)
            return upload

    def upload(self, root, relative_path, chunks, *, size, sha256):
        with self.begin_upload(root, relative_path, size=size) as upload:
            for chunk in chunks:
                upload.write(chunk)
            return upload.commit(sha256)

    def open_download(self, root, relative_path, *, checksum=True):
        with self._lock:
            parent = self._parent(root, relative_path)
            target = parent / _relative(relative_path)[-1]
            before = _regular(target)
            if self.max_file_size is not None and before.st_size > self.max_file_size:
                raise ValueError("File exceeds the configured file limit")
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_BINARY", 0))
            stream = os.fdopen(descriptor, "rb")
        try:
            if Revision.of(os.fstat(stream.fileno())) != Revision.of(before):
                raise FileChangedError("File changed while opening it")
            if not checksum:
                with self._lock:
                    info = self._metadata(root, relative_path, None, before)
                return Download(self, info, target, stream)
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(self.chunk_size):
                size += len(chunk)
                if size > before.st_size:
                    raise FileChangedError("File grew while hashing it")
                digest.update(chunk)
            try:
                current = Revision.of(_regular(target))
            except (FileNotFoundError, ValueError):
                raise FileChangedError("File was removed or replaced while hashing it") from None
            if (size != before.st_size or current != Revision.of(before)
                    or Revision.of(os.fstat(stream.fileno())) != Revision.of(before)):
                raise FileChangedError("File changed while hashing it")
            stream.seek(0)
            with self._lock:
                info = self._metadata(root, relative_path, digest.hexdigest(), before)
            return Download(self, info, target, stream)
        except BaseException:
            stream.close()
            raise

    def remove(self, root, relative_path, *, revision=None, missing_ok=False):
        with self._lock:
            try:
                parent = self._parent(root, relative_path)
                target = parent / _relative(relative_path)[-1]
                value = _regular(target)
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            if revision is not None and Revision.of(value) != revision:
                raise FileChangedError("Refusing to delete a changed file")
            target.unlink()
            self._invalidate(root)

    def close(self):
        """Abort only active uploads. Never remove completed native files."""
        with self._lock:
            uploads = tuple(self._uploads)
        failure = None
        for upload in uploads:
            try:
                upload.abort()
            except OSError as error:
                failure = failure or error
        if failure is not None:
            raise failure


class Upload:
    def __init__(self, store, root, relative_path, parent, size):
        self.store, self.root, self.relative_path = store, root, relative_path
        self.size, self.written = size, 0
        self._parent = parent
        value = parent.stat()
        self._parent_identity = (value.st_dev, value.st_ino)
        self._partial = parent / (".comani-" + uuid.uuid4().hex + ".part")
        self._stream = self._partial.open("xb")
        value = os.fstat(self._stream.fileno())
        self._identity = (value.st_dev, value.st_ino)
        self._hash = hashlib.sha256()
        self._closed = False
        self._lock = threading.RLock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.abort()

    def write(self, chunk):
        with self._lock:
            return self._write(chunk)

    def _write(self, chunk):
        try:
            if self._closed:
                raise ValueError("Upload is already closed")
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("Upload chunks must be bytes-like")
            chunk = memoryview(chunk).cast("B")
            if len(chunk) > self.store.chunk_size or self.written + len(chunk) > self.size:
                raise ValueError("Upload chunk or actual file size exceeds the declared limit")
            if self._stream.write(chunk) != len(chunk):
                raise OSError("Incomplete write to upload staging file")
            self._hash.update(chunk)
            self.written += len(chunk)
        except BaseException:
            self.abort()
            raise

    def commit(self, sha256):
        with self._lock:
            return self._commit(sha256)

    def _commit(self, sha256):
        try:
            if self._closed:
                raise ValueError("Upload is already closed")
            digest = self._hash.hexdigest()
            if self.written != self.size or digest != sha256:
                raise ValueError("Upload size or SHA256 does not match")
            self._stream.flush()
            os.fsync(self._stream.fileno())
            expected = Revision.of(os.fstat(self._stream.fileno()))
            self._stream.close()
            with self.store._lock:
                parent = self.store._parent(self.root, self.relative_path)
                value = parent.stat()
                if (value.st_dev, value.st_ino) != self._parent_identity:
                    raise FileChangedError("Upload parent directory changed")
                if Revision.of(_regular(self._partial)) != expected:
                    raise FileChangedError("Upload staging file changed")
                self.store._conflicts(self.root, self.relative_path)
                target = parent / _relative(self.relative_path)[-1]
                try:
                    os.link(self._partial, target)
                except NotImplementedError as error:
                    raise UnsupportedFileSystem(
                        "Atomic create-only file publication requires same-filesystem hard links"
                    ) from error
                except OSError as error:
                    unsupported = {errno.EXDEV, errno.ENOSYS, errno.EPERM,
                                   errno.EOPNOTSUPP, errno.ENOTSUP}
                    if error.errno in unsupported:
                        raise UnsupportedFileSystem(
                            "Atomic create-only file publication requires same-filesystem hard links"
                        ) from error
                    raise
                try:
                    _unlink_owned(self._partial, self._identity)
                    info = self.store._metadata(self.root, self.relative_path,
                                                digest, _regular(target))
                except BaseException:
                    _unlink_owned(target, self._identity)
                    raise
                self._closed = True
                self.store._uploads.discard(self)
                self.store._invalidate(self.root)
                return info
        except BaseException:
            self.abort()
            raise

    def abort(self):
        with self._lock:
            self._abort()

    def _abort(self):
        if not self._closed:
            try:
                self._stream.close()
            finally:
                try:
                    _unlink_owned(self._partial, self._identity)
                finally:
                    self._closed = True
                    with self.store._lock:
                        self.store._uploads.discard(self)


class Download:
    def __init__(self, store, metadata, target, stream):
        self.store, self.metadata = store, metadata
        self._target, self._stream = target, stream

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def chunks(self):
        digest, size = hashlib.sha256(), 0
        try:
            while chunk := self._stream.read(self.store.chunk_size):
                size += len(chunk)
                if size > self.metadata.size:
                    raise FileChangedError("File grew during download")
                digest.update(chunk)
                yield chunk
            try:
                current = Revision.of(_regular(self._target))
            except (FileNotFoundError, ValueError):
                raise FileChangedError("File was removed or replaced during download") from None
            if (size != self.metadata.size
                    or (self.metadata.sha256 is not None
                        and digest.hexdigest() != self.metadata.sha256)
                    or Revision.of(os.fstat(self._stream.fileno())) != self.metadata.revision
                    or current != self.metadata.revision):
                raise FileChangedError("File changed during download")
            self.metadata = replace(self.metadata, sha256=digest.hexdigest())
        finally:
            self.close()

    def close(self):
        self._stream.close()
