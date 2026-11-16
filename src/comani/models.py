"""Static models functions bound to the current explicit Client scope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Coroutine

from ._context import invoke_async as _invoke_async
from .files import FileRoot, RemoteFile
from .management import (
    ManagementOperation,
    ModelArtifact,
    ModelManifest,
    ModelSnapshot,
    ModelSource,
)


async def resolve(source, *, revision=None, file_id=None, token=None) -> ModelSource:
    """Resolve provider facts locally, using local or explicitly supplied credentials."""
    from ._model_providers import ProviderQueries
    from .management import _decode

    value = await ProviderQueries(token=token).resolve(source, revision=revision, file_id=file_id)
    return _decode("models", "resolve", value)


async def versions(source, *, token=None):
    """Query provider versions locally without a Client or daemon."""
    from ._model_providers import ProviderQueries
    from .management import _decode

    return _decode("models", "versions", await ProviderQueries(token=token).versions(source))


async def manifest(source, *, revision=None, allow=None, ignore=None, token=None) -> ModelManifest:
    """Resolve a portable model snapshot locally without remote disk access."""
    from ._model_providers import ProviderQueries
    from .management import _decode

    value = await ProviderQueries(token=token).manifest(
        source, revision=revision, allow=allow, ignore=ignore
    )
    return _decode("models", "manifest", value)


def categories() -> Coroutine[Any, Any, Any]:
    return _invoke_async(("models", "categories"))


def list(*, category=None, root: FileRoot | None = None, prefix=None) -> Coroutine[Any, Any, Any]:
    return _invoke_async(("models", "list"), category=category, root=root, prefix=prefix)


def begin_download(
    artifact: ModelArtifact | Mapping | str,
    *,
    root: FileRoot | None = None,
    category: str | None = None,
    path=None,
    reuse=False,
    timeout=None,
    transport="auto",
    connections=8,
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("models", "begin_download"),
        artifact,
        root=root,
        category=category,
        path=path,
        reuse=reuse,
        timeout=timeout,
        transport=transport,
        connections=connections,
    )


def download(
    artifact: ModelArtifact | Mapping | str,
    *,
    root: FileRoot | None = None,
    category: str | None = None,
    path=None,
    reuse=False,
    timeout=None,
    transport="auto",
    connections=8,
) -> Coroutine[Any, Any, RemoteFile]:
    """Acquire into an explicit root or a category's native lookup order.

    A category reuses the first visible file at the selected relative path only
    when reuse=True and its SHA256 matches; otherwise new files use its first
    native root. The returned RemoteFile identifies the actual destination.
    """
    return _invoke_async(
        ("models", "download"),
        artifact,
        root=root,
        category=category,
        path=path,
        reuse=reuse,
        timeout=timeout,
        transport=transport,
        connections=connections,
    )


def begin_snapshot(
    manifest: ModelManifest | Mapping,
    *,
    root: FileRoot,
    path: str,
    publish: str,
    transport="auto",
    connections=8,
    timeout=None,
    max_total_bytes=None,
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("models", "begin_snapshot"),
        manifest,
        root=root,
        path=path,
        publish=publish,
        transport=transport,
        connections=connections,
        timeout=timeout,
        max_total_bytes=max_total_bytes,
    )


def snapshot(
    manifest: ModelManifest | Mapping,
    *,
    root: FileRoot,
    path: str,
    publish: str,
    transport="auto",
    connections=8,
    timeout=None,
    max_total_bytes=None,
) -> Coroutine[Any, Any, ModelSnapshot]:
    return _invoke_async(
        ("models", "snapshot"),
        manifest,
        root=root,
        path=path,
        publish=publish,
        transport=transport,
        connections=connections,
        timeout=timeout,
        max_total_bytes=max_total_bytes,
    )


def remove(file: RemoteFile) -> Coroutine[Any, Any, Any]:
    return _invoke_async(("models", "remove"), file)


def begin_remove(file: RemoteFile) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("models", "begin_remove"), file)
