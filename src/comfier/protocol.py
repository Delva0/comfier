"""Small JSON value vocabulary. No ComfyUI or tensor imports are needed here."""

from __future__ import annotations

import base64
import io
import pickle
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import FunctionType
from typing import Any, Callable

import cloudpickle

PROTOCOL_VERSION = 1
SUBMIT_BATCH_VERSION = 1
SUBMIT_BATCH_MAX_CALLS = 64
SUBMIT_BATCH_MAX_BYTES = 8 * 1024 * 1024
CALL_OBSERVATION_VERSION = 1
PYTHON_INPUTS_VERSION = 1
# Protocol v1 identifiers survive the public package rename.
PYTHON_NODE = "ComaniPython"
PICKLE_CONTENT_TYPE = "application/vnd.comani.pickle"
BINARY_FETCH_VERSION = 1
LZ4_PICKLE_CONTENT_TYPE = "application/vnd.comani.pickle+lz4"
BINARY_FETCH_LZ4_VERSION = 1
LZ4_CODEC = "lz4-frame-v1"
LZ4_ACCEPT_HEADER = "X-Comani-Accept-Codec"
PICKLE_LENGTH_HEADER = "X-Comani-Uncompressed-Length"


def pack_pickle(value: Any) -> dict:
    return {"codec": "pickle", "data": base64.b64encode(pickle.dumps(value)).decode("ascii")}


def unpack_pickle(payload: dict | bytes) -> Any:
    """Decode the JSON envelope or a negotiated raw pickle HTTP body."""
    if isinstance(payload, bytes):
        return pickle.loads(payload)
    if payload.get("codec") != "pickle":
        raise ValueError(f"Unsupported object codec: {payload.get('codec')!r}")
    return pickle.loads(base64.b64decode(payload["data"], validate=True))


def encode_tree(value: Any, encode_special: Callable[[Any], Any] | None = None) -> Any:
    """Encode containers explicitly so a literal [node_id, slot] stays literal."""
    if encode_special is not None:
        special = encode_special(value)
        if special is not NotImplemented:
            return special
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return {"$list": [encode_tree(v, encode_special) for v in value]}
    if isinstance(value, tuple):
        return {"$tuple": [encode_tree(v, encode_special) for v in value]}
    if isinstance(value, Mapping):
        return {"$dict": [[encode_tree(k, encode_special), encode_tree(v, encode_special)]
                          for k, v in value.items()]}
    return {"$value": pack_pickle(value)}


def decode_tree(value: Any, resolve_special: Callable[[dict], Any] | None = None) -> Any:
    if not isinstance(value, dict):
        return value
    if resolve_special is not None:
        special = resolve_special(value)
        if special is not NotImplemented:
            return special
    if set(value) == {"$list"}:
        return [decode_tree(v, resolve_special) for v in value["$list"]]
    if set(value) == {"$tuple"}:
        return tuple(decode_tree(v, resolve_special) for v in value["$tuple"])
    if set(value) == {"$dict"}:
        return {decode_tree(k, resolve_special): decode_tree(v, resolve_special)
                for k, v in value["$dict"]}
    if set(value) == {"$value"}:
        return unpack_pickle(value["$value"])
    return {k: decode_tree(v, resolve_special) for k, v in value.items()}


@dataclass(frozen=True)
class _PythonLayout:
    descriptor: dict

    def wire(self, handshake):
        from .code import _check_pickle_compatibility
        from .errors import RemoteError

        version = handshake.get("capabilities", {}).get("python_inputs")
        if type(version) is not int or version != PYTHON_INPUTS_VERSION:
            raise RemoteError("This daemon does not support lossless Python inputs; update the daemon")
        if self.descriptor.get("cloudpickle") is not None:
            _check_pickle_compatibility(handshake, description="Dynamic Python input transfer")
        return self.descriptor


class _ArgumentPickler(cloudpickle.CloudPickler):
    """Keep pickle's memo; expose SDK references only through native ports."""

    def __init__(self, stream, is_port):
        super().__init__(stream, protocol=pickle.DEFAULT_PROTOCOL)
        self.is_port = is_port
        self.ports = {}
        self.port_names = {}
        self.dynamic = False
        self.by_value_modules = cloudpickle.list_registry_pickle_by_value()
        # Cloudpickle's public dispatch table extends pickle's reducers. Mark
        # its extensions as version-dependent without serializing twice.
        self.dispatch_table = {
            kind: self._track(reducer)
            if getattr(reducer, "__module__", "").startswith("cloudpickle") else reducer
            for kind, reducer in self.dispatch_table.items()
        }

    def _track(self, reducer):
        def reduce(value):
            self.dynamic = True
            return reducer(value)
        return reduce

    def reducer_override(self, value):
        module = getattr(value, "__module__", None) if isinstance(value, (type, FunctionType)) else None
        by_value = module is not None and any(
            module == name or module.startswith(name + ".") for name in self.by_value_modules
        )
        if (isinstance(value, (type, FunctionType))
                and module != "__main__" and not by_value):
            try:
                # This probes only a class/function global, never the argument
                # graph. Prefer pickle's import-by-name behavior even for
                # builtins such as SimpleNamespace which cloudpickle rewrites.
                pickle.dumps(value, protocol=pickle.DEFAULT_PROTOCOL)
            except (pickle.PicklingError, TypeError, AttributeError):
                pass
            else:
                return NotImplemented
        reduction = super().reducer_override(value)
        if reduction is not NotImplemented:
            self.dynamic = True
        return reduction

    def persistent_id(self, value):
        if self.is_port is None or not self.is_port(value):
            return None
        identity = id(value)
        if identity not in self.port_names:
            name = f"v{len(self.ports)}"
            self.port_names[identity] = name
            self.ports[name] = value
        return ("port", self.port_names[identity])


def lift_arguments(args: tuple, kwargs: dict, is_port=None) -> tuple[_PythonLayout, dict]:
    """Serialize the entire argument graph once, lifting SDK handles to ports."""
    stream = io.BytesIO()
    pickler = _ArgumentPickler(stream, is_port)
    try:
        pickler.dump((args, kwargs))
    except (pickle.PicklingError, TypeError, AttributeError, RecursionError) as error:
        raise TypeError(f"Cannot serialize Python args/inputs: {error}") from error
    descriptor = {"pickle": base64.b64encode(stream.getvalue()).decode("ascii"),
                  "version": PYTHON_INPUTS_VERSION}
    if pickler.dynamic:
        descriptor.update(python=list(sys.version_info[:3]), cloudpickle=cloudpickle.__version__)
    return _PythonLayout(descriptor), pickler.ports


def restore_arguments(layout: dict | _PythonLayout, ports: dict) -> tuple[tuple, dict]:
    if isinstance(layout, _PythonLayout):
        layout = layout.descriptor
    if "pickle" in layout:
        if type(layout.get("version")) is not int or layout["version"] != PYTHON_INPUTS_VERSION:
            raise ValueError("Unsupported Python input layout version")
        if (layout.get("cloudpickle") is not None
                and (layout.get("python") != list(sys.version_info[:3])
                     or layout["cloudpickle"] != cloudpickle.__version__)):
            raise ValueError("Dynamic Python inputs require matching Python and cloudpickle versions")

        class ArgumentsUnpickler(pickle.Unpickler):
            def persistent_load(self, identity):
                if (not isinstance(identity, tuple) or len(identity) != 2
                        or identity[0] != "port" or identity[1] not in ports):
                    raise pickle.UnpicklingError("Invalid Python input port")
                return ports[identity[1]]

        return ArgumentsUnpickler(io.BytesIO(base64.b64decode(layout["pickle"], validate=True))).load()

    def resolve(value):
        if set(value) == {"$port"}:
            return ports[value["$port"]]
        return NotImplemented

    return decode_tree(layout["args"], resolve), decode_tree(layout["kwargs"], resolve)
