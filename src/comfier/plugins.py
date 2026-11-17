"""Static plugins functions bound to the current explicit Client scope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Coroutine

from ._context import invoke_async as _invoke_async
from .management import ManagementMetadata, ManagementOperation, PluginChanges, PluginInfo


async def versions(package: str):
    """Query the Comfy registry locally, without a Client or daemon."""
    from ._model_providers import ProviderQueries
    from .management import _decode

    return _decode("plugins", "versions", await ProviderQueries().registry_versions(package))



def begin_install_archive(
    archive: Mapping, *, no_deps=False, timeout=None
) -> Coroutine[Any, Any, ManagementOperation]:
    """Install SHA-pinned ZIP directories using the current daemon."""
    return _invoke_async(
        ("plugins", "begin_install_archive"), archive, no_deps=no_deps, timeout=timeout
    )


def install_archive(
    archive: Mapping, *, no_deps=False, timeout=None
) -> Coroutine[Any, Any, PluginChanges]:
    """no_deps skips requirements; install.py may still execute, as with Git."""
    return _invoke_async(
        ("plugins", "install_archive"), archive, no_deps=no_deps, timeout=timeout
    )


def begin_install_git(
    repository: str, *, commit=None, name=None, no_deps=False, timeout=None
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("plugins", "begin_install_git"),
        repository,
        commit=commit,
        name=name,
        no_deps=no_deps,
        timeout=timeout,
    )


def install_git(
    repository: str, *, commit=None, name=None, no_deps=False, timeout=None
) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "install_git"),
        repository,
        commit=commit,
        name=name,
        no_deps=no_deps,
        timeout=timeout,
    )


def begin_update_git(
    package: str, *, commit=None, no_deps=False, timeout=None
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("plugins", "begin_update_git"), package, commit=commit, no_deps=no_deps, timeout=timeout
    )


def update_git(
    package: str, *, commit=None, no_deps=False, timeout=None
) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "update_git"), package, commit=commit, no_deps=no_deps, timeout=timeout
    )


def backend() -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(("plugins", "backend"))


def list() -> Coroutine[Any, Any, tuple[PluginInfo, ...]]:
    return _invoke_async(("plugins", "list"))


def begin_install(
    packages, *, version=None, no_deps=False, timeout=None, manager_version=None
) -> Coroutine[Any, Any, Any]:
    return _invoke_async(
        ("plugins", "begin_install"),
        packages,
        version=version,
        no_deps=no_deps,
        timeout=timeout,
        manager_version=manager_version,
    )


def install(
    packages, *, version=None, no_deps=False, timeout=None, manager_version=None
) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "install"),
        packages,
        version=version,
        no_deps=no_deps,
        timeout=timeout,
        manager_version=manager_version,
    )


def begin_update(
    packages, *, version=None, no_deps=False, timeout=None, manager_version=None
) -> Coroutine[Any, Any, Any]:
    return _invoke_async(
        ("plugins", "begin_update"),
        packages,
        version=version,
        no_deps=no_deps,
        timeout=timeout,
        manager_version=manager_version,
    )


def update(
    packages, *, version=None, no_deps=False, timeout=None, manager_version=None
) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "update"),
        packages,
        version=version,
        no_deps=no_deps,
        timeout=timeout,
        manager_version=manager_version,
    )


def begin_enable(packages, *, version=None, timeout=None) -> Coroutine[Any, Any, Any]:
    return _invoke_async(("plugins", "begin_enable"), packages, version=version, timeout=timeout)


def enable(packages, *, version=None, timeout=None) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(("plugins", "enable"), packages, version=version, timeout=timeout)


def begin_disable(packages, *, version=None, timeout=None) -> Coroutine[Any, Any, Any]:
    return _invoke_async(("plugins", "begin_disable"), packages, version=version, timeout=timeout)


def disable(packages, *, version=None, timeout=None) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(("plugins", "disable"), packages, version=version, timeout=timeout)


def begin_uninstall(packages, *, timeout=None, manager_version=None) -> Coroutine[Any, Any, Any]:
    return _invoke_async(
        ("plugins", "begin_uninstall"), packages, timeout=timeout, manager_version=manager_version
    )


def uninstall(
    packages, *, timeout=None, manager_version=None
) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "uninstall"), packages, timeout=timeout, manager_version=manager_version
    )


def remove(packages, *, timeout=None, manager_version=None) -> Coroutine[Any, Any, PluginChanges]:
    return _invoke_async(
        ("plugins", "remove"), packages, timeout=timeout, manager_version=manager_version
    )
