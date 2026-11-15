"""Optional lossless compression of complete, generic cloudpickle bodies."""

from __future__ import annotations

import sys

from .errors import RemoteError

try:
    import lz4.frame as _lz4
except ImportError:
    _lz4 = None

LZ4_MIN_BYTES = 1024 * 1024


def has_lz4():
    return _lz4 is not None


def compress_pickle(payload):
    """Attempt fast compression only for large bodies; require 1% savings."""
    if _lz4 is None or len(payload) < LZ4_MIN_BYTES:
        return None
    compressed = _lz4.compress(payload, compression_level=0, content_checksum=True, store_size=True)
    return compressed if len(compressed) * 100 <= len(payload) * 99 else None


def decompress_pickle(payload, raw_length):
    """Accept exactly one checksummed frame with matching declared byte sizes."""
    if _lz4 is None:
        raise RemoteError("Daemon returned LZ4 without a local decoder")
    if (not isinstance(raw_length, str) or not raw_length or len(raw_length) > 20
            or not raw_length.isascii() or not raw_length.isdecimal()):
        raise RemoteError("Daemon returned an invalid LZ4 uncompressed length")
    expected = int(raw_length)
    if not 0 < expected <= sys.maxsize:
        raise RemoteError("Daemon returned an invalid LZ4 uncompressed length")
    try:
        info = _lz4.get_frame_info(payload)
        if info["skippable"] or not info["content_checksum"] or info["content_size"] != expected:
            raise ValueError("LZ4 frame metadata does not match the response")
        value, consumed = _lz4.decompress(payload, return_bytes_read=True)
        if consumed != len(payload) or len(value) != expected:
            raise ValueError("LZ4 response has trailing data or an incorrect length")
    except (RuntimeError, ValueError, OverflowError):
        raise RemoteError("Daemon returned an invalid LZ4 pickle response") from None
    return value
