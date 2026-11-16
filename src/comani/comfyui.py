"""Static comfyui functions bound to the current explicit Client scope."""

from __future__ import annotations

from typing import Any, Coroutine

from ._context import invoke_async as _invoke_async
from .management import ComfyUIInfo, ManagementMetadata, ManagementOperation


def info() -> Coroutine[Any, Any, ComfyUIInfo]:
    return _invoke_async(("comfyui", "info"))


def config() -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(("comfyui", "config"))


def logs() -> Coroutine[Any, Any, Any]:
    return _invoke_async(("comfyui", "logs"))


def configure(**fields) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(("comfyui", "configure"), **fields)


def begin_configure(**fields) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("comfyui", "begin_configure"), **fields)


def begin_prepare_runtime(package: str) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("comfyui", "begin_prepare_runtime"), package)


def prepare_runtime(package: str) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(("comfyui", "prepare_runtime"), package)


def begin_create_environment(
    path: str, *, python=None, timeout=None
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("comfyui", "begin_create_environment"), path, python=python, timeout=timeout
    )


def create_environment(
    path: str, *, python=None, timeout=None
) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(("comfyui", "create_environment"), path, python=python, timeout=timeout)


def begin_prepare_tools(
    *,
    cli_revision=None,
    cli_version=None,
    manager_version=None,
    hub_version=None,
    skip_cli=False,
    skip_manager=False,
    timeout=None,
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(
        ("comfyui", "begin_prepare_tools"),
        cli_revision=cli_revision,
        cli_version=cli_version,
        manager_version=manager_version,
        hub_version=hub_version,
        skip_cli=skip_cli,
        skip_manager=skip_manager,
        timeout=timeout,
    )


def prepare_tools(
    *,
    cli_revision=None,
    cli_version=None,
    manager_version=None,
    hub_version=None,
    skip_cli=False,
    skip_manager=False,
    timeout=None,
) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(
        ("comfyui", "prepare_tools"),
        cli_revision=cli_revision,
        cli_version=cli_version,
        manager_version=manager_version,
        hub_version=hub_version,
        skip_cli=skip_cli,
        skip_manager=skip_manager,
        timeout=timeout,
    )


def begin_install(
    *,
    version=None,
    commit=None,
    skip_torch=True,
    skip_requirements=False,
    skip_manager=False,
    cli_version=None,
) -> Coroutine[Any, Any, Any]:
    return _invoke_async(
        ("comfyui", "begin_install"),
        version=version,
        commit=commit,
        skip_torch=skip_torch,
        skip_requirements=skip_requirements,
        skip_manager=skip_manager,
        cli_version=cli_version,
    )


def install(
    *,
    version=None,
    commit=None,
    skip_torch=True,
    skip_requirements=False,
    skip_manager=False,
    cli_version=None,
) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(
        ("comfyui", "install"),
        version=version,
        commit=commit,
        skip_torch=skip_torch,
        skip_requirements=skip_requirements,
        skip_manager=skip_manager,
        cli_version=cli_version,
    )


def begin_update(
    *,
    version=None,
    commit=None,
    skip_torch=True,
    skip_requirements=False,
    skip_manager=False,
    cli_version=None,
) -> Coroutine[Any, Any, Any]:
    return _invoke_async(
        ("comfyui", "begin_update"),
        version=version,
        commit=commit,
        skip_torch=skip_torch,
        skip_requirements=skip_requirements,
        skip_manager=skip_manager,
        cli_version=cli_version,
    )


def update(
    *,
    version=None,
    commit=None,
    skip_torch=True,
    skip_requirements=False,
    skip_manager=False,
    cli_version=None,
) -> Coroutine[Any, Any, ManagementMetadata]:
    return _invoke_async(
        ("comfyui", "update"),
        version=version,
        commit=commit,
        skip_torch=skip_torch,
        skip_requirements=skip_requirements,
        skip_manager=skip_manager,
        cli_version=cli_version,
    )


def start() -> Coroutine[Any, Any, ComfyUIInfo]:
    return _invoke_async(("comfyui", "start"))


def begin_start() -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("comfyui", "begin_start"))


def stop(*, wait=True, force=False) -> Coroutine[Any, Any, ComfyUIInfo]:
    return _invoke_async(("comfyui", "stop"), wait=wait, force=force)


def begin_stop(*, wait=True, force=False) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("comfyui", "begin_stop"), wait=wait, force=force)


def restart(*, wait=True, force=False) -> Coroutine[Any, Any, ComfyUIInfo]:
    return _invoke_async(("comfyui", "restart"), wait=wait, force=force)


def begin_restart(*, wait=True, force=False) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("comfyui", "begin_restart"), wait=wait, force=force)
