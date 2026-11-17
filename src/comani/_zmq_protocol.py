"""The bounded compute envelope shared by IPC and TCP adapters."""

from __future__ import annotations

import ipaddress
import math
from pathlib import Path

METHODS = frozenset({
    "handshake", "resume", "submission_window", "runtime_info", "file_invalidate",
    "submit", "submit_batch", "wait_result", "fetch", "release", "forget_calls",
    "close_session", "artifact_put", "heartbeat",
})
VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024**2
MAX_RESPONSE_BYTES = 512 * 1024**2


def dependency():
    try:
        import zmq
        import zmq.asyncio
    except ImportError:
        raise ImportError("ZMQ compute requires the optional dependency: pip install 'comani[zmq]'") from None
    return zmq


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + " must be positive and finite")
    return value


def limit(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


def endpoint(value, *, encrypted=False, bind=False):
    if not isinstance(value, str):
        raise ValueError("A ZMQ endpoint must be a string")
    if value.startswith("ipc://"):
        if not Path(value[6:]).is_absolute():
            raise ValueError("An IPC endpoint requires an absolute filesystem path")
    elif value.startswith("tcp://"):
        address = value[6:]
        try:
            host, port = address.rsplit(":", 1)
            if not host or (":" in host and not (host.startswith("[") and host.endswith("]"))):
                raise ValueError
            host = host.removeprefix("[").removesuffix("]")
            if port == "*":
                if not bind:
                    raise ValueError
            elif not 0 < int(port) <= 65535:
                raise ValueError
            if host == "*" and not bind:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("A TCP endpoint requires a host and port") from None
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local and not encrypted:
            raise ValueError("Non-loopback ZMQ TCP requires CURVE encryption and a pinned server key")
    else:
        raise ValueError("ZMQ compute supports only ipc:// and tcp:// endpoints")
    return value


def curve_key(value, name):
    if value is None:
        return None
    try:
        encoded = value.encode("ascii") if isinstance(value, str) else value
    except UnicodeEncodeError:
        encoded = None
    alphabet = b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?&<>()[]{}@%$#"
    if not isinstance(encoded, bytes) or not (
            len(encoded) == 32 or (len(encoded) == 40 and all(item in alphabet for item in encoded))):
        raise ValueError(name + " must be a 40-character Z85 key or 32 raw bytes")
    return encoded


def capabilities(result):
    """Never advertise HTTP file routes or codecs on the compute socket."""
    result = dict(result)
    result["capabilities"] = dict(result.get("capabilities", {}))
    for name in ("files", "files_resume", "binary_fetch_lz4", "management"):
        result["capabilities"].pop(name, None)
    result["capabilities"]["compute_zmq"] = VERSION
    result.pop("file_limits", None)
    result.pop("file_transfer_epoch", None)
    return result
