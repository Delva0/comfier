from __future__ import annotations

import threading
import uuid
import weakref
from dataclasses import dataclass
from typing import Any

from comfier.errors import Expired, RemoteError, Skipped
from comfier.errors import SessionClosed as ClosedSession


class RefExpired(Expired):
    pass


class NodeSkipped(Skipped):
    pass


class SessionClosed(ClosedSession):
    pass


class CallFailed(RemoteError):
    pass


@dataclass
class Entry:
    session: str
    owner: str
    data: Any = None
    cache: Any = None
    key: Any = None
    identity: int = 0
    version: str = ""
    slot: int = 0
    leases: int = 0
    released: bool = False
    is_list: bool = False


class ObjectStore:
    def __init__(self, epoch: str, *, max_entries=None):
        self.epoch = epoch
        self.max_entries = max_entries
        self.entries: dict[str, Entry] = {}
        self.lock = threading.RLock()

    def _capacity(self, count=1):
        if self.max_entries is not None and len(self.entries) + count > self.max_entries:
            error = CallFailed("The runtime object recovery capacity is full; release unused references")
            error.code = "RecoveryCapacity"
            raise error

    def own(self, session: str, data: list, *, leases: int = 0, is_list=False) -> dict:
        token = uuid.uuid4().hex
        with self.lock:
            self._capacity()
            self.entries[token] = Entry(session, "owned", data=data, leases=leases, is_list=is_list)
        return {"id": token, "epoch": self.epoch, "owner": "owned"}

    def borrow(self, session: str, cache: Any, key: Any, entry: Any, slot: int, *, is_list=False, leases=0) -> dict:
        token = uuid.uuid4().hex
        with self.lock:
            self._capacity()
            version = cache._comani_entry_versions.get(key, "")
            self.entries[token] = Entry(session, "borrowed", data=entry.outputs[slot] if leases else None, cache=weakref.ref(cache), key=key, identity=id(entry), version=version, slot=slot, is_list=is_list, leases=leases)
        return {"id": token, "epoch": self.epoch, "owner": "borrowed"}

    def get(self, session: str, token: str, epoch: str | None = None, *, pinned=False) -> list:
        with self.lock:
            if epoch is not None and epoch != self.epoch:
                raise RefExpired("The runtime restarted; this reference has expired")
            item = self.entries.get(token)
            if item is None or item.session != session:
                raise RefExpired("The remote reference was released or expired")
            if item.owner == "owned":
                return item.data
            if pinned and item.leases and item.data is not None:
                return item.data
            cache = item.cache()
            entry = None if cache is None else cache.cache.get(item.key)
            if entry is None or id(entry) != item.identity or cache._comani_entry_versions.get(item.key) != item.version:
                raise RefExpired("ComfyUI evicted this workflow output")
            return entry.outputs[item.slot]

    def acquire(self, session: str, token: str) -> None:
        with self.lock:
            item = self.entries.get(token)
            if item is None or item.session != session:
                raise RefExpired("The remote reference was released")
            if item.owner == "borrowed" and not item.leases:
                item.data = self.get(session, token)
            item.leases += 1

    def value(self, session, token, epoch=None):
        with self.lock:
            data = self.get(session, token, epoch)
            return list(data) if self.entries[token].is_list else value_from_data(data)

    def release(self, session: str, tokens: list[str], *, lease: bool = False) -> None:
        with self.lock:
            for token in tokens:
                item = self.entries.get(token)
                if item is None or item.session != session:
                    continue
                if lease:
                    item.leases = max(0, item.leases - 1)
                    if item.owner == "borrowed" and not item.leases:
                        item.data = None
                else:
                    item.released = True
                if item.released and not item.leases:
                    del self.entries[token]

    def close_session(self, session: str) -> None:
        with self.lock:
            self.release(session, [key for key, entry in self.entries.items() if entry.session == session])


def value_from_data(data: list) -> Any:
    return data[0] if len(data) == 1 else list(data)


def walk_refs(value: Any):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        else:
            for child in value.values():
                yield from walk_refs(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from walk_refs(child)
