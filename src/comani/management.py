"""Out-of-band management. These methods perform real I/O inside graph scopes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any

from .errors import (
    CommandError,
    DaemonRestarted,
    OperationCancelled,
    OperationError,
    OperationTimeout,
    RemoteError,
    SessionClosed,
)
from .files import FileRoot, RemoteFile, _remote
from .transport import retryable_transport_error


def _plain(value):
    if isinstance(value, ManagementMetadata):
        return _plain(value.data)
    if isinstance(value, ModelArtifact):
        return _plain(value.to_wire())
    if is_dataclass(value):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError("Management arguments must be local JSON values or management metadata")


@dataclass(frozen=True)
class ManagementMetadata(Mapping):
    """Local versioned metadata; extra server fields remain accessible by key."""

    data: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.data[key]

    def __iter__(self):
        return iter(self.data)

    def __len__(self):
        return len(self.data)

    def __getattr__(self, name):
        try:
            return self.data[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    operation_id: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class ComfyUIInfo:
    comfy_root: str = ""
    runtime_python: str = ""
    workspace: str = ""
    state: str = "unknown"
    pid: int | None = None
    runtime_epoch: str | None = None
    installed: bool = False
    error: str | None = None
    roots_generation: str | None = None
    roots_state: str = "unknown"
    config: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModelArtifact:
    provider: str
    source: dict
    filename: str
    size: int | None = None
    sha256: str | None = None
    revision: str | None = None
    metadata: dict = field(default_factory=dict)
    git_sha1: str | None = None
    _download_url: str | None = field(default=None, repr=False, compare=False)

    def to_public_dict(self):
        """Stable provider facts suitable for logs; omit private transport data."""
        return {item.name: getattr(self, item.name) for item in fields(self)
                if not item.name.startswith("_")}

    def to_wire(self):
        result = self.to_public_dict()
        if self._download_url is not None:
            result["source"] = {**self.source, "url": self._download_url}
        return result


@dataclass(frozen=True)
class ModelSource:
    provider: str
    source: Any
    revision: str | None
    files: tuple[ModelArtifact, ...]

    def to_public_dict(self):
        return {"provider": self.provider, "source": self.source, "revision": self.revision,
                "files": [artifact.to_public_dict() for artifact in self.files]}


@dataclass(frozen=True)
class ModelManifest:
    provider: str
    source: Any
    revision: str
    files: tuple[ModelArtifact, ...]
    allow: tuple[str, ...] | None = None
    ignore: tuple[str, ...] | None = None
    total_size: int | None = None

    def to_public_dict(self):
        return {"provider": self.provider, "source": self.source, "revision": self.revision,
                "files": [artifact.to_public_dict() for artifact in self.files],
                "allow": self.allow, "ignore": self.ignore, "total_size": self.total_size}


@dataclass(frozen=True)
class ModelSnapshot:
    publish: str
    revision: str
    files: tuple[RemoteFile, ...]
    items: tuple[ManagementMetadata, ...]
    complete: bool
    partial_changes: bool
    path: str | None = None
    root: FileRoot | None = None
    native_visibility: str = "unknown"


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str = ""
    path: str = ""
    enabled: bool = True
    version: str | None = None
    commit: str | None = None
    repository: str | None = None
    source: Any = None
    live_imported: bool | None = None
    archive: dict | None = None


@dataclass(frozen=True)
class PluginChanges:
    items: tuple[ManagementMetadata, ...]
    restart_required: bool
    partial_changes: Any = None


@dataclass(frozen=True)
class OperationHandle:
    operation_id: str
    daemon_epoch: str
    namespace: str
    action: str
    check: bool | None = None


@dataclass(frozen=True)
class OperationStatus:
    operation_id: str
    daemon_epoch: str
    namespace: str
    action: str
    state: str
    created_at: Any = None
    started_at: Any = None
    finished_at: Any = None
    progress: Any = None
    result: Any = None
    error: Any = None
    log_start: int = 0
    log_end: int = 0

    @property
    def done(self):
        return self.state in {"succeeded", "failed", "cancelled", "partial", "timed_out"}


@dataclass(frozen=True)
class LogEntry:
    cursor: int
    time: Any
    stream: str
    text: str


@dataclass(frozen=True)
class LogPage:
    entries: tuple[LogEntry, ...]
    next_cursor: int
    log_start: int
    done: bool


def _fields(cls, value):
    return cls(**{key: item for key, item in value.items() if key in cls.__dataclass_fields__})


def _command(value, operation_id=""):
    truncated = value.get("truncated", {})
    return CommandResult(value.get("exit_code", 0), value.get("stdout", ""),
        value.get("stderr", ""), value.get("duration", 0.0),
        value.get("operation_id", operation_id), bool(truncated.get("stdout")),
        bool(truncated.get("stderr")))


def _decode(namespace, action, value, operation_id=""):
    if namespace == "comfyui" and action in {"info", "start", "stop", "restart"}:
        fields = ComfyUIInfo.__dataclass_fields__
        return ComfyUIInfo(**{key: item for key, item in value.items() if key in fields},
                           extra={key: item for key, item in value.items() if key not in fields})
    if namespace == "bash":
        return _command(value, operation_id)
    if namespace == "models":
        if action == "download":
            return _remote(value.get("file", value))
        if action == "resolve":
            return ModelSource(value["provider"], value.get("source"), value.get("revision"),
                               tuple(_fields(ModelArtifact, item) for item in value.get("files", [])))
        if action == "manifest":
            return ModelManifest(value["provider"], value.get("source"), value["revision"],
                tuple(_fields(ModelArtifact, item) for item in value.get("files", [])),
                tuple(value["allow"]) if value.get("allow") is not None else None,
                tuple(value["ignore"]) if value.get("ignore") is not None else None, value.get("total_size"))
        if action == "snapshot":
            items = value.get("files", [])
            root = _fields(FileRoot, value["root"]) if isinstance(value.get("root"), dict) else None
            return ModelSnapshot(value["publish"], value["revision"],
                tuple(_remote(item["file"]) for item in items),
                tuple(ManagementMetadata(item) for item in items), bool(value.get("complete")),
                bool(value.get("partial_changes")), value.get("path"), root,
                value.get("native_visibility", "unknown"))
    if namespace == "plugins":
        if action == "list":
            items = value.get("items", value.get("plugins", [])) if isinstance(value, dict) else value
            return tuple(_fields(PluginInfo, item) for item in items)
        if isinstance(value, dict) and "items" in value:
            return PluginChanges(tuple(ManagementMetadata(item) for item in value["items"]),
                                 bool(value.get("restart_required")), value.get("partial_changes"))
    return ManagementMetadata(value) if isinstance(value, dict) else value


async def _rpc(client, method, *, expected_daemon_epoch=None, **params):
    await client.start()
    if not client._managed:
        raise RemoteError("This daemon does not support out-of-band management")
    epoch = expected_daemon_epoch or client.management_handshake["daemon_epoch"]
    delay = 0.05
    while True:
        if client.closed or client._closing:
            raise SessionClosed("The client closed while observing a management operation")
        try:
            return await client.transport.management_request(method, **params)
        except Exception as error:
            if not retryable_transport_error(error):
                raise
        while True:
            if client.closed or client._closing:
                raise SessionClosed("The client closed while observing a management operation")
            try:
                handshake = await client.transport.management_request("handshake")
                if handshake.get("daemon_epoch") != epoch:
                    raise DaemonRestarted("The management daemon restarted; the original outcome is unknown")
                break
            except Exception as error:
                if not retryable_transport_error(error):
                    raise
                await asyncio.sleep(delay)
                delay = min(2.0, delay * 2)


class ManagementOperation:
    """Observe one accepted task. Cancelling a local await leaves it running."""

    def __init__(self, client, handle, *, check=None):
        self._client, self._handle = client, handle
        self._check = (handle.check if handle.check is not None else True) if check is None else check

    @property
    def id(self):
        return self._handle.operation_id

    def export_handle(self) -> OperationHandle:
        return self._handle

    async def status(self) -> OperationStatus:
        value = await _rpc(self._client, "management_status", expected_daemon_epoch=self._handle.daemon_epoch,
                           operation_id=self.id, daemon_epoch=self._handle.daemon_epoch)
        return _fields(OperationStatus, value)

    async def result(self):
        while True:
            value = await _rpc(self._client, "management_result", expected_daemon_epoch=self._handle.daemon_epoch,
                operation_id=self.id, daemon_epoch=self._handle.daemon_epoch, timeout=20.0)
            status = _fields(OperationStatus, value)
            if status.done:
                break
        value = status.result
        if status.state != "succeeded":
            details = {"operation_id": self.id, "state": status.state,
                       "result": value, "error": status.error}
            error_code = status.error.get("code") if isinstance(status.error, dict) else None
            kind = (OperationCancelled if status.state == "cancelled" else
                    OperationTimeout if status.state == "timed_out" or error_code == "OperationTimeout" else
                    CommandError if self._handle.namespace == "bash" and isinstance(value, dict)
                    and value.get("exit_code", 0) != 0 else OperationError)
            if kind is CommandError:
                details["result"] = _command(value, self.id)
            raise kind(f"Management operation {self.id} {status.state}", details=details)
        result = _decode(self._handle.namespace, self._handle.action, value, self.id)
        if isinstance(result, CommandResult) and self._check and result.exit_code:
            raise CommandError(f"Command exited with status {result.exit_code}",
                               details={"operation_id": self.id, "result": result})
        if (self._handle.namespace == "comfyui" and self._handle.action in {"start", "stop", "restart"}
                and result.runtime_epoch != self._client.epoch):
            await self._client._reset_compute()
        if self._handle.namespace == "comfyui" and self._handle.action in {"start", "stop", "restart"}:
            self._client._cache_node_metadata(result.extra.get("node_metadata"))
        return result

    async def log_page(self, *, after=0, follow=False, timeout=20.0, limit=256) -> LogPage:
        value = await _rpc(self._client, "management_logs", expected_daemon_epoch=self._handle.daemon_epoch,
            operation_id=self.id, daemon_epoch=self._handle.daemon_epoch,
            after=after, follow=follow, timeout=timeout, limit=limit)
        return LogPage(tuple(_fields(LogEntry, item) for item in value.get("entries", [])),
                       value["next_cursor"], value.get("log_start", 0), bool(value.get("done")))

    async def logs(self, *, after=0, follow=True):
        while True:
            page = await self.log_page(after=after, follow=follow)
            for entry in page.entries:
                yield entry
            after = page.next_cursor
            if len(page.entries) < 256 and (not follow or page.done):
                return

    async def cancel(self) -> OperationStatus:
        value = await _rpc(self._client, "management_cancel", expected_daemon_epoch=self._handle.daemon_epoch,
                           operation_id=self.id, daemon_epoch=self._handle.daemon_epoch)
        return _fields(OperationStatus, value)


class Operations:
    def __init__(self, client):
        self._client = client

    async def get(self, *, handle: OperationHandle | Mapping) -> ManagementOperation:
        if isinstance(handle, Mapping):
            handle = OperationHandle(**handle)
        if not isinstance(handle, OperationHandle):
            raise TypeError("handle must be an exported OperationHandle")
        operation = ManagementOperation(self._client, handle)
        await operation.status()
        return operation


class _Namespace:
    namespace = ""

    def __init__(self, client):
        self._client = client

    async def _read(self, action, **params):
        value = await _rpc(self._client, "management_read", namespace=self.namespace,
                           action=action, params=_plain(params))
        return _decode(self.namespace, action, value)

    async def _begin(self, action, *, check=True, **params):
        await self._client.start()
        epoch = self._client.management_handshake.get("daemon_epoch")
        if not epoch:
            raise RemoteError("This daemon does not support out-of-band management")
        window = await _rpc(self._client, "management_window", expected_daemon_epoch=epoch)
        if window.get("daemon_epoch") != epoch:
            raise DaemonRestarted("The management daemon changed before operation submission")
        # Fix and validate all replay bytes before the first side-effecting request.
        request = {"namespace": self.namespace, "action": action,
                   "params": json.loads(json.dumps(_plain(params), allow_nan=False)),
                   "key": uuid.uuid4().hex, "daemon_epoch": epoch, "window": window["window"]}
        if self.namespace == "bash":
            request["params"]["check"] = check
        value = await _rpc(self._client, "management_begin", expected_daemon_epoch=epoch, **request)
        handle = OperationHandle(value["operation_id"], epoch, self.namespace, action,
                                 check if self.namespace == "bash" else None)
        return ManagementOperation(self._client, handle, check=check)

    async def _run(self, action, **params):
        operation = await self._begin(action, **params)
        return await operation.result()


class Bash(_Namespace):
    namespace = "bash"

    async def begin(self, script: str, *, cwd=None, env=None, timeout=None, check=True) -> ManagementOperation:
        if not isinstance(script, str):
            raise TypeError("script must be a local string")
        if type(check) is not bool:
            raise TypeError("check must be bool")
        return await self._begin("run", script=script, cwd=cwd, env=env, timeout=timeout, check=check)

    async def run(self, script: str, *, cwd=None, env=None, timeout=None, check=True) -> CommandResult:
        return await (await self.begin(script, cwd=cwd, env=env, timeout=timeout, check=check)).result()


class ComfyUI(_Namespace):
    namespace = "comfyui"

    async def info(self) -> ComfyUIInfo:
        result = await self._read("info")
        if self._client.epoch and result.runtime_epoch != self._client.epoch:
            await self._client._reset_compute()
        self._client._cache_node_metadata(result.extra.get("node_metadata"))
        return result

    async def config(self) -> ManagementMetadata:
        return await self._read("config")

    async def logs(self):
        return await self._read("logs")

    async def configure(self, **fields) -> ManagementMetadata:
        return await self._run("configure", **fields)

    async def begin_configure(self, **fields) -> ManagementOperation:
        return await self._begin("configure", **fields)

    async def begin_prepare_runtime(self, package: str) -> ManagementOperation:
        return await self._begin("prepare_runtime", package=package)

    async def prepare_runtime(self, package: str) -> ManagementMetadata:
        return await (await self.begin_prepare_runtime(package)).result()

    async def begin_create_environment(self, path: str, *, python=None, timeout=None) -> ManagementOperation:
        return await self._begin("create_environment", path=path, python=python, timeout=timeout)

    async def create_environment(self, path: str, *, python=None, timeout=None) -> ManagementMetadata:
        return await (await self.begin_create_environment(path, python=python, timeout=timeout)).result()

    async def begin_prepare_tools(self, *, cli_revision=None, cli_version=None,
                                  manager_version=None, hub_version=None, skip_cli=False,
                                  skip_manager=False, timeout=None) -> ManagementOperation:
        return await self._begin("prepare_tools", cli_revision=cli_revision,
            cli_version=cli_version, manager_version=manager_version, hub_version=hub_version,
            skip_cli=skip_cli, skip_manager=skip_manager, timeout=timeout)

    async def prepare_tools(self, *, cli_revision=None, cli_version=None, manager_version=None,
                            hub_version=None, skip_cli=False, skip_manager=False,
                            timeout=None) -> ManagementMetadata:
        return await (await self.begin_prepare_tools(cli_revision=cli_revision,
            cli_version=cli_version, manager_version=manager_version, hub_version=hub_version,
            skip_cli=skip_cli, skip_manager=skip_manager, timeout=timeout)).result()

    async def begin_install(self, *, version=None, commit=None, skip_torch=True,
                            skip_requirements=False, skip_manager=False, cli_version=None):
        return await self._begin("install", version=version, commit=commit, skip_torch=skip_torch,
            skip_requirements=skip_requirements, skip_manager=skip_manager, cli_version=cli_version)

    async def install(self, *, version=None, commit=None, skip_torch=True,
                      skip_requirements=False, skip_manager=False, cli_version=None) -> ManagementMetadata:
        return await (await self.begin_install(version=version, commit=commit, skip_torch=skip_torch,
            skip_requirements=skip_requirements, skip_manager=skip_manager, cli_version=cli_version)).result()

    async def begin_update(self, *, version=None, commit=None, skip_torch=True,
                           skip_requirements=False, skip_manager=False, cli_version=None):
        return await self._begin("update", version=version, commit=commit, skip_torch=skip_torch,
            skip_requirements=skip_requirements, skip_manager=skip_manager, cli_version=cli_version)

    async def update(self, *, version=None, commit=None, skip_torch=True,
                     skip_requirements=False, skip_manager=False, cli_version=None) -> ManagementMetadata:
        return await (await self.begin_update(version=version, commit=commit, skip_torch=skip_torch,
            skip_requirements=skip_requirements, skip_manager=skip_manager, cli_version=cli_version)).result()

    async def start(self) -> ComfyUIInfo:
        return await (await self.begin_start()).result()

    async def begin_start(self) -> ManagementOperation:
        return await self._begin("start")

    async def stop(self, *, wait=True, force=False) -> ComfyUIInfo:
        return await (await self.begin_stop(wait=wait, force=force)).result()

    async def begin_stop(self, *, wait=True, force=False) -> ManagementOperation:
        return await self._begin("stop", wait=wait, force=force)

    async def restart(self, *, wait=True, force=False) -> ComfyUIInfo:
        return await (await self.begin_restart(wait=wait, force=force)).result()

    async def begin_restart(self, *, wait=True, force=False) -> ManagementOperation:
        return await self._begin("restart", wait=wait, force=force)


class Plugins(_Namespace):
    namespace = "plugins"

    async def begin_install_archive(self, archive: Mapping, *, no_deps=False,
                                    timeout=None) -> ManagementOperation:
        """Install SHA-pinned ZIP package directories; install.py may execute."""
        return await self._begin("install_archive", archive=archive,
                                 no_deps=no_deps, timeout=timeout)

    async def install_archive(self, archive: Mapping, *, no_deps=False,
                              timeout=None) -> PluginChanges:
        """Fetch/install in the daemon; no_deps skips requirements, not install.py."""
        return await (await self.begin_install_archive(archive, no_deps=no_deps,
                                                       timeout=timeout)).result()

    async def begin_install_git(self, repository: str, *, commit=None, name=None,
                                no_deps=False, timeout=None) -> ManagementOperation:
        return await self._begin("install_git", repository=repository, commit=commit,
                                 name=name, no_deps=no_deps, timeout=timeout)

    async def install_git(self, repository: str, *, commit=None, name=None,
                          no_deps=False, timeout=None) -> PluginChanges:
        return await (await self.begin_install_git(repository, commit=commit, name=name,
                                                   no_deps=no_deps, timeout=timeout)).result()

    async def begin_update_git(self, package: str, *, commit=None, no_deps=False,
                               timeout=None) -> ManagementOperation:
        return await self._begin("update_git", package=package, commit=commit,
                                 no_deps=no_deps, timeout=timeout)

    async def update_git(self, package: str, *, commit=None, no_deps=False,
                         timeout=None) -> PluginChanges:
        return await (await self.begin_update_git(package, commit=commit,
                                                  no_deps=no_deps, timeout=timeout)).result()

    async def backend(self) -> ManagementMetadata:
        return await self._read("backend")

    async def list(self) -> tuple[PluginInfo, ...]:
        return await self._read("list")

    async def begin_install(self, packages, *, version=None, no_deps=False, timeout=None, manager_version=None):
        return await self._begin("install", packages=packages, version=version, no_deps=no_deps,
                                 timeout=timeout, manager_version=manager_version)

    async def install(self, packages, *, version=None, no_deps=False, timeout=None, manager_version=None) -> PluginChanges:
        return await (await self.begin_install(packages, version=version, no_deps=no_deps,
                                               timeout=timeout, manager_version=manager_version)).result()

    async def begin_update(self, packages, *, version=None, no_deps=False, timeout=None, manager_version=None):
        return await self._begin("update", packages=packages, version=version, no_deps=no_deps,
                                 timeout=timeout, manager_version=manager_version)

    async def update(self, packages, *, version=None, no_deps=False, timeout=None, manager_version=None) -> PluginChanges:
        return await (await self.begin_update(packages, version=version, no_deps=no_deps,
                                              timeout=timeout, manager_version=manager_version)).result()

    async def begin_enable(self, packages, *, version=None, timeout=None):
        return await self._begin("enable", packages=packages, version=version, timeout=timeout)

    async def enable(self, packages, *, version=None, timeout=None) -> PluginChanges:
        return await (await self.begin_enable(packages, version=version, timeout=timeout)).result()

    async def begin_disable(self, packages, *, version=None, timeout=None):
        return await self._begin("disable", packages=packages, version=version, timeout=timeout)

    async def disable(self, packages, *, version=None, timeout=None) -> PluginChanges:
        return await (await self.begin_disable(packages, version=version, timeout=timeout)).result()

    async def begin_uninstall(self, packages, *, timeout=None, manager_version=None):
        return await self._begin("uninstall", packages=packages, timeout=timeout,
                                 manager_version=manager_version)

    async def uninstall(self, packages, *, timeout=None, manager_version=None) -> PluginChanges:
        return await (await self.begin_uninstall(packages, timeout=timeout,
                                                manager_version=manager_version)).result()

    async def remove(self, packages, *, timeout=None, manager_version=None) -> PluginChanges:
        return await self.uninstall(packages, timeout=timeout, manager_version=manager_version)


class Models(_Namespace):
    namespace = "models"

    async def categories(self):
        return await self._read("categories")

    async def list(self, *, category=None, root: FileRoot | None = None, prefix=None):
        return await self._read("list", category=category, root=root, prefix=prefix)

    async def begin_download(self, artifact: ModelArtifact | Mapping | str, *,
                             root: FileRoot | None = None, category: str | None = None,
                             path=None, reuse=False, timeout=None, transport="auto", connections=8) -> ManagementOperation:
        destination = {"root": root}
        if category is not None:
            destination["category"] = category
        return await self._begin("download", artifact=artifact, **destination, path=path,
                                 reuse=reuse, timeout=timeout, transport=transport, connections=connections)

    async def download(self, artifact: ModelArtifact | Mapping | str, *,
                       root: FileRoot | None = None, category: str | None = None,
                       path=None, reuse=False, timeout=None, transport="auto", connections=8) -> RemoteFile:
        """Acquire into exactly one explicit root or native model category.

        Category selection follows ComfyUI's search order: reuse the visible
        existing path after SHA256 verification, or publish to the first root.
        An earlier conflicting file is never bypassed to reuse a later match.
        """
        return await (await self.begin_download(artifact, root=root, category=category, path=path,
            reuse=reuse, timeout=timeout, transport=transport, connections=connections)).result()

    async def begin_snapshot(self, manifest: ModelManifest | Mapping, *, root: FileRoot, path: str,
                             publish: str, transport="auto", connections=8, timeout=None,
                             max_total_bytes=None) -> ManagementOperation:
        if publish not in {"directory", "files"}:
            raise ValueError("publish must explicitly select 'directory' or 'files'")
        return await self._begin("snapshot", manifest=manifest, root=root, path=path, publish=publish,
                                 transport=transport, connections=connections, timeout=timeout,
                                 max_total_bytes=max_total_bytes)

    async def snapshot(self, manifest: ModelManifest | Mapping, *, root: FileRoot, path: str,
                       publish: str, transport="auto", connections=8, timeout=None,
                       max_total_bytes=None) -> ModelSnapshot:
        return await (await self.begin_snapshot(manifest, root=root, path=path, publish=publish,
            transport=transport, connections=connections, timeout=timeout,
            max_total_bytes=max_total_bytes)).result()

    async def remove(self, file: RemoteFile):
        return await self._run("remove", file=file)

    async def begin_remove(self, file: RemoteFile) -> ManagementOperation:
        return await self._begin("remove", file=file)
