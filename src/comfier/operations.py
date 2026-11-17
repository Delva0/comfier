"""Static operations functions bound to the current explicit Client scope."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Coroutine

from ._context import invoke_async as _invoke_async
from .management import ManagementOperation, OperationHandle


def get(*, handle: OperationHandle | Mapping) -> Coroutine[Any, Any, ManagementOperation]:
    return _invoke_async(("operations", "get"), handle=handle)
