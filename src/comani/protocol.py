"""Small JSON value vocabulary. No ComfyUI or tensor imports are needed here."""

from __future__ import annotations

import base64
import pickle
from collections.abc import Mapping
from typing import Any, Callable

PROTOCOL_VERSION = 1
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


def lift_arguments(args: tuple, kwargs: dict) -> tuple[dict, dict]:
    """Lift nested argument leaves to native node ports, preserving containers."""
    ports = {}

    def lift(value):
        if isinstance(value, list):
            return {"$list": [lift(item) for item in value]}
        if isinstance(value, tuple):
            return {"$tuple": [lift(item) for item in value]}
        if isinstance(value, dict):
            return {"$dict": [[encode_tree(key), lift(item)] for key, item in value.items()]}
        name = f"v{len(ports)}"
        ports[name] = value
        return {"$port": name}

    return {"args": lift(args), "kwargs": lift(kwargs)}, ports


def restore_arguments(layout: dict, ports: dict) -> tuple[tuple, dict]:
    def resolve(value):
        if set(value) == {"$port"}:
            return ports[value["$port"]]
        return NotImplemented

    return decode_tree(layout["args"], resolve), decode_tree(layout["kwargs"], resolve)
