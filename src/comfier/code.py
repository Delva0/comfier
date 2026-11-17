"""Prepare explicitly selected code artifacts; never translate pickle to source."""

from __future__ import annotations

import hashlib
import inspect
import io
import keyword
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import cloudpickle

from .errors import CodeCompatibilityError


@dataclass(frozen=True)
class CodeArtifact:
    kind: str
    data: bytes
    entrypoint: str | None = None
    package_root: str | None = None

    @property
    def artifact_id(self):
        return hashlib.sha256(self.data).hexdigest()

    def descriptor(self):
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "entrypoint": self.entrypoint,
            "package_root": self.package_root,
            "python": list(sys.version_info[:3]) if self.kind == "pickle" else None,
            "cloudpickle": cloudpickle.__version__ if self.kind == "pickle" else None,
        }

    def check_compatibility(self, handshake):
        if self.kind != "pickle":
            return
        _check_pickle_compatibility(handshake)


def _check_pickle_compatibility(handshake, *, description="Callable transfer"):
    remote_python = tuple(handshake.get("python", ()))
    remote_pickle = handshake.get("cloudpickle")
    if remote_python != tuple(sys.version_info[:3]) or remote_pickle != cloudpickle.__version__:
        raise CodeCompatibilityError(
            f"{description} requires exactly matching Python and cloudpickle versions; "
            "explicitly select a self-contained source file/package and ordinary data for another environment",
            details={"local_python": list(sys.version_info[:3]), "remote_python": list(remote_python),
                     "local_cloudpickle": cloudpickle.__version__, "remote_cloudpickle": remote_pickle},
        )


def prepare_code(function=None, *, code=None, source=None, entry=None):
    if function is not None:
        if code is not None or source is not None or entry is not None:
            raise TypeError("Choose a callable or explicit source/entry, not both")
        if not callable(function):
            raise TypeError("The first argument must be callable")
        _check_captured_references(function)
        return CodeArtifact("pickle", cloudpickle.dumps(function))
    if code is not None:
        if source is not None:
            raise TypeError("Choose code=source_text or source=path, not both")
        if not isinstance(code, str):
            raise TypeError("code must be a Python source string")
        if not entry:
            raise TypeError("Inline source code requires entry=entrypoint")
        return CodeArtifact("file", code.encode("utf-8"), str(entry))
    if source is None or not entry:
        raise TypeError("Source code requires source=path and entry=entrypoint")
    path = Path(source).expanduser().resolve(strict=True)
    if path.is_file():
        data = path.read_bytes()
        data.decode("utf-8")
        return CodeArtifact("file", data, str(entry))
    if not path.is_dir():
        raise ValueError("Source must be a file or package directory")
    root = path.name
    if not root.isidentifier() or keyword.iskeyword(root):
        raise ValueError("A source package directory must have a valid Python package name")
    module, separator, function = str(entry).partition(":")
    if not separator or not module or not function:
        raise ValueError("A package entrypoint must be module:callable")
    if module != root and not module.startswith(root + "."):
        module = root + "." + module
    entrypoint = module + ":" + function
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for item in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
            relative = item.relative_to(path)
            if (not item.is_file() or any(part in ignored for part in relative.parts)
                    or item.suffix in {".pyc", ".pyo"} or item.name.startswith(".env")):
                continue
            if not item.resolve().is_relative_to(path):
                raise ValueError(f"Package symlink escapes source directory: {relative}")
            info = zipfile.ZipInfo((Path(path.name) / relative).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, item.read_bytes())
    return CodeArtifact("package", buffer.getvalue(), entrypoint, root)


def _check_captured_references(function):
    from ._graph import GraphInput, GraphRef
    from .refs import OutputFuture, RemoteRef

    seen = set()

    def visit(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, (RemoteRef, OutputFuture, GraphRef, GraphInput)):
            raise TypeError("A callable captured a remote/graph reference; pass it through inputs instead")
        if isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for item in value:
                visit(item)

    if inspect.isfunction(function):
        captured = inspect.getclosurevars(function)
        visit(captured.nonlocals)
        visit(captured.globals)
        visit(function.__defaults__)
        visit(function.__kwdefaults__)
