"""Explicit checked ZIP sources, installed by the daemon without Manager guesses."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path

from comfier.files import _io

from ..files import FileChangedError, _directory, _relative
from .git_plugins import _step
from .models import ModelService
from .operations import ManagementError
from .plugins import custom_node_roots, listing, package_id
from .providers import checked_url
from .tools import dependency_changes, python_metadata, tool_env

RECEIPT = ".comani-archive.json"


def manifest(value):
    if not isinstance(value, dict) or set(value) - {"url", "sha256", "size", "packages"}:
        raise ManagementError("InvalidArgument", "Archive source requires url, sha256, optional size and packages")
    url = checked_url(value.get("url"))
    digest, size = value.get("sha256"), value.get("size")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ManagementError("InvalidArgument", "Select an archive with an explicit SHA256")
    if size is not None and (type(size) is not int or size < 0):
        raise ManagementError("InvalidArgument", "Archive size must be a nonnegative byte count")
    packages = value.get("packages")
    if not isinstance(packages, dict) or not 1 <= len(packages) <= 100:
        raise ManagementError("InvalidArgument", "Select between 1 and 100 explicit archive packages")
    selected, names = {}, set()
    for name, subdirectory in packages.items():
        package_id(name)
        if (len(_relative(name)) != 1 or name.startswith(".")
                or name.endswith(".disabled") or name.casefold() in names):
            raise ManagementError("InvalidArgument", "Select unique, visible custom-node directory names")
        names.add(name.casefold())
        if not isinstance(subdirectory, str):
            raise ManagementError("InvalidArgument", "Package subdirectories must be explicit ZIP-relative paths")
        selected[name] = () if subdirectory == "." else tuple(_relative(subdirectory.rstrip("/")))
    return url, digest.lower(), size, selected


def extract(stage, destination, packages):
    """Never use extractall: validate names/types, bound disk use and stream files."""
    stage.check()
    try:
        with zipfile.ZipFile(stage.path) as archive:
            members = archive.infolist()
            if len(members) > 10000:
                raise ManagementError("InvalidPluginArchive", "ZIP exceeds 10000 entries")
            seen, selected, total = set(), [], 0
            for member in members:
                # orig_filename preserves NULs that ZipInfo.filename truncates.
                raw = member.orig_filename
                parts = tuple(_relative(raw[:-1] if member.is_dir() else raw))
                key = "/".join(parts).casefold()
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (key in seen or ".git" in parts or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or (kind == stat.S_IFDIR and not member.is_dir()) or member.flag_bits & 1):
                    raise ManagementError("InvalidPluginArchive", "ZIP has duplicate, encrypted or non-regular entries")
                seen.add(key)
                for name, prefix in packages.items():
                    if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
                        relative = parts[len(prefix):]
                        if relative == (RECEIPT,):
                            raise ManagementError("InvalidPluginArchive", "ZIP occupies the installer receipt path")
                        selected.append((name, relative, member, mode))
                        if not member.is_dir():
                            total += member.file_size
            if total > shutil.disk_usage(destination).free:
                raise ManagementError("DiskFull", "Insufficient available disk space for selected ZIP contents")
            for name in packages:
                (destination / name).mkdir(mode=0o700)
            for name, relative, member, mode in selected:
                target = destination.joinpath(name, *relative)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     0o700 if mode & 0o111 else 0o600)
                with os.fdopen(descriptor, "wb") as output, archive.open(member) as source:
                    received = 0
                    while chunk := source.read(1024 * 1024):
                        received += len(chunk)
                        if received > member.file_size:
                            raise ManagementError("InvalidPluginArchive", "ZIP entry exceeded its declared size")
                        output.write(chunk)
                    if received != member.file_size:
                        raise ManagementError("InvalidPluginArchive", "ZIP entry was incomplete")
            for name in packages:
                if not (destination / name / "__init__.py").is_file():
                    raise ManagementError("InvalidPluginArchive", "Each selected package must contain __init__.py")
    except (ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError):
        raise ManagementError("InvalidPluginArchive", "ZIP structure or entry data is invalid") from None
    stage.check()


def _identity(value):
    return value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode


def _check_directory(root, descriptor, identity):
    try:
        _directory(root)
        if _identity(root.lstat()) != identity or _identity(os.fstat(descriptor)) != identity:
            raise ValueError("Directory identity changed")
    except (ValueError, OSError):
        raise ManagementError("RootsChanged", "Selected custom-node directory changed during installation") from None


def _publish(source, name, root, descriptor, identity):
    before = source.lstat()
    _check_directory(root, descriptor, identity)
    # The held directory fd anchors the write even if its pathname is replaced
    # between the identity check and renameat2. Never follow a replacement link.
    ModelService._rename_directory(source, Path(name), destination_fd=descriptor)
    try:
        _check_directory(root, descriptor, identity)
    except BaseException:
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise FileChangedError("Refusing to reclaim a replaced plugin directory") from None
        ModelService._rename_directory(Path(name), source, source_fd=descriptor)
        raise


def _conflicts(roots, names):
    for root in roots:
        try:
            _directory(root)
        except FileNotFoundError:
            continue
        for name in names:
            for candidate in (root / name, root / (name + ".disabled"), root / ".disabled" / name):
                if candidate.exists() or candidate.is_symlink():
                    raise ManagementError("FileExists", "A plugin with this destination name already exists")


async def install(supervisor, context, resolver, *, archive, no_deps=False, timeout=None):
    url, digest, size, packages = manifest(archive)
    if type(no_deps) is not bool:
        raise ManagementError("InvalidArgument", "no_deps must be a boolean; install.py can still execute")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise ManagementError("UnsupportedFileSystem", "Archive installation requires no-follow directory handles")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        roots = await custom_node_roots(supervisor)
        if not roots:
            raise ManagementError("RootsUnavailable", "Native custom_nodes roots are unavailable")
        _conflicts(roots, packages)
        root = _directory(roots[0], create=True)
        metadata = await python_metadata(supervisor.config.runtime_python, inventory=True)
        env = tool_env(supervisor.config, metadata)
        steps, items = [], []
        result = {"items": items, "steps": steps, "restart_required": True, "partial_changes": False}
        context.partial(result)
        # Stage outside every import root on the destination filesystem.
        parent = root.parent
        while any(parent == configured or configured in parent.parents for configured in roots):
            if parent == parent.parent:
                raise ManagementError("UnsupportedFileSystem", "No staging location exists outside custom_nodes roots")
            parent = parent.parent
        if parent.stat().st_dev != root.stat().st_dev:
            raise ManagementError("UnsupportedFileSystem", "Atomic archive installation requires the destination filesystem")
        temporary = Path(tempfile.mkdtemp(prefix=".comani-plugin-", dir=parent))
        artifact = None
        try:
            artifact = resolver.bind({"provider": "url", "source": {"url": url},
                "filename": "plugin.zip", "size": size, "sha256": digest})
            resolver.retain(artifact)
            with ExitStack() as handles:
                descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                handles.callback(os.close, descriptor)
                identity = _identity(os.fstat(descriptor))
                payload = temporary / "payload.zip"
                payload.touch(mode=0o600, exist_ok=False)
                service = ModelService(supervisor, resolver)
                store = await supervisor.get_file_store()
                stage, verified = await service._fetch(context, artifact, payload,
                    max_bytes=store.max_file_size, timeout=timeout)
                tree = temporary / "packages"
                tree.mkdir(mode=0o700)
                context.progress(phase="extracting", bytes=stage.size, total=stage.size)
                await _io(extract, stage, tree, packages)
                current_roots = await custom_node_roots(supervisor)
                if root not in current_roots:
                    raise ManagementError("RootsChanged", "Selected custom-node root is no longer configured")
                _check_directory(root, descriptor, identity)
                _conflicts(current_roots, packages)
                source = resolver.public(artifact)["source"]["url"]
                for name, prefix in packages.items():
                    receipt = {"url": source, "sha256": digest, "size": stage.size,
                               "subdirectory": "/".join(prefix) or "."}
                    (tree / name / RECEIPT).write_text(json.dumps(receipt, sort_keys=True) + "\n")
                    _publish(tree / name, name, root, descriptor, identity)
                    item = {"package": name, "status": "partial", "path": str(root / name),
                            "archive": receipt, "dependencies": "pending", "install_script_may_have_run": False}
                    items.append(item)
                    result["partial_changes"] = True
                    context.partial(result)
                for item in items:
                    target = Path(item["path"])
                    context.progress(phase="dependencies", package=item["package"])
                    requirement, installer = target / "requirements.txt", target / "install.py"
                    if not no_deps and requirement.is_file():
                        await _step(supervisor, context, [supervisor.config.runtime_python, "-m", "pip", "install", "-r", str(requirement)],
                                    target, steps, env, timeout)
                    if installer.is_file():
                        item["install_script_may_have_run"] = True
                        await _step(supervisor, context, [supervisor.config.runtime_python, str(installer)],
                                    target, steps, env, timeout)
                await _step(supervisor, context, [supervisor.config.runtime_python, "-m", "pip", "check"],
                            root, steps, env, timeout)
                installed = {item["path"]: item for item in await listing(supervisor)}
                for item in items:
                    item["after"] = installed.get(item["path"])
                    if item["after"] is None:
                        raise ManagementError("PluginChangeFailed", "An installed archive package is no longer visible")
                    item["status"], item["dependencies"] = "applied", "checked"
                result["dependency_changes"] = dependency_changes(metadata,
                    await python_metadata(supervisor.config.runtime_python, inventory=True))
                return result
        finally:
            context.partial(result)
            if artifact is not None:
                resolver.release(artifact)
            await _io(shutil.rmtree, temporary)
