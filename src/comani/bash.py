"""Static bash functions bound to the current explicit Client scope."""

from __future__ import annotations

from typing import Any, Coroutine

from ._context import invoke_async as _invoke_async
from .management import CommandResult, ManagementOperation


def begin(
    script: str, *, cwd=None, env=None, timeout=None, check=True
) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("bash", "begin"), script, cwd=cwd, env=env, timeout=timeout, check=check)


def run(
    script: str, *, cwd=None, env=None, timeout=None, check=True
) -> Coroutine[Any, Any, CommandResult]:
    return _invoke_async(("bash", "run"), script, cwd=cwd, env=env, timeout=timeout, check=check)
