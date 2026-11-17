"""Task-local routing for explicitly entered Client scopes."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import weakref
from dataclasses import dataclass

from .errors import SessionClosed


@dataclass
class _Binding:
    client_ref: weakref.ReferenceType
    loop: asyncio.AbstractEventLoop
    thread: int
    active: bool = True

    def client(self):
        if not self.active:
            raise RuntimeError("The current comfier Client scope has exited")
        client = self.client_ref()
        if client is None or client.closed or client._closing:
            raise SessionClosed("The current comfier Client session is closed")
        if threading.get_ident() != self.thread:
            raise RuntimeError("Use module-level comfier calls in the Client scope's thread")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "Use module-level comfier calls in the Client scope's event loop"
            ) from None
        if loop is not self.loop:
            raise RuntimeError("Use module-level comfier calls in the Client scope's event loop")
        return client


_current = contextvars.ContextVar("comfier_current_client", default=None)


def enter_client(client):
    binding = _Binding(weakref.ref(client), asyncio.get_running_loop(), threading.get_ident())
    return binding, _current.set(binding)


def exit_client(binding, token):
    if _current.get() is not binding:
        raise RuntimeError("Exit the current Client context in the Context that entered it")
    try:
        _current.reset(token)
    except ValueError:
        raise RuntimeError("Exit the Client context in the Context that entered it") from None
    binding.active = False


def _binding():
    binding = _current.get()
    if binding is None:
        raise RuntimeError("No current comfier Client; use 'async with comfier.Client(...)'")
    binding.client()
    return binding


def current_client():
    return _binding().client()


def invoke_async(path, /, *args, **kwargs):
    """Capture a scope now, while keeping an ordinary create_task-compatible coroutine."""
    binding = _binding()
    return _invoke(binding, path, args, kwargs)


async def _invoke(binding, path, args, kwargs):
    target = binding.client()
    for name in path:
        target = getattr(target, name)
    return await target(*args, **kwargs)
