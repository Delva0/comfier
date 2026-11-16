"""Declared output bundles and explicit local-demand slot projection."""

from __future__ import annotations

import dis
import sys

from .errors import InvalidReference, NotSubmitted
from .refs import _OutputTuple


def _unpack_count(frame):
    for instruction in dis.get_instructions(frame.f_code):
        if instruction.offset < frame.f_lasti or instruction.opname == "EXTENDED_ARG":
            continue
        if instruction.opname == "UNPACK_SEQUENCE":
            return instruction.argval
        break
    raise TypeError("Unchecked node outputs support fixed unpacking; await them for the actual result")


class _OutputProxy:
    def __init__(self, call, *, graph=None):
        self._call = call
        self._graph = graph

    def __iter__(self):
        count = _unpack_count(sys._getframe(1))
        return (self._call.output(slot) for slot in range(count))

    def __await__(self):
        return self._ready().__await__()

    async def _ready(self):
        if self._graph is not None:
            self._call._check_capture()
            return self
        outputs = await self._call
        if not outputs:
            return None
        return outputs[0] if len(outputs) == 1 else _OutputTuple(outputs, state=self._call._state)

    def _wire(self, client):
        if self._graph is not None:
            raise NotSubmitted("Unpack captured node outputs into explicit slots before using them as inputs")
        state = self._call._state
        if state.client_ref() is not client:
            raise InvalidReference("References cannot cross client sessions")
        return state.wire_ref()

    def __bool__(self):
        raise TypeError("Unpack or await node outputs before using them in local control flow")

    def __reduce__(self):
        raise TypeError("Unpack node outputs before passing their individual references")


def declared_outputs(call, count, *, graph=None):
    if count == 0:
        return None
    if count == 1:
        return call.output(0)
    if graph is not None:
        return _OutputTuple((call.output(i) for i in range(count)), graph_call=call)
    return _OutputTuple((call.output(i) for i in range(count)), state=call._state)
