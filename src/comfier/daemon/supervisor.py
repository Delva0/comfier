"""A small out-of-band supervisor for exactly one native runtime process.

Tensor and file response bodies cross the selected compute adapter or HTTP
file proxy without deserializing them in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import aiohttp

from comfier.files import FILE_CHUNK_SIZE, _io

from .files import FileStore


class SupervisorError(RuntimeError):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


@dataclass(frozen=True)
class RuntimeConfig:
    comfy_root: str
    runtime_python: str = sys.executable
    workspace: str = "."
    cpu: bool = False
    custom_nodes: bool = True
    comfy_args: tuple[str, ...] = ()
    offline_grace: float = 3600
    max_sessions: int = 256
    max_call_records: int = 1000000
    max_objects: int = 100000
    max_file_bytes: int | None = None
    transfer_idle_timeout: float = 3600
    transfer_limit: int = 128
    transfer_owner_limit: int = 8
    startup_timeout: float = 180
    compute_transport: str = "http"

    def __post_init__(self):
        if self.compute_transport not in {"http", "zmq"}:
            raise ValueError("compute_transport must be 'http' or 'zmq'")
        if self.compute_transport == "zmq" and os.name != "posix":
            raise ValueError("The supervised ZMQ worker currently requires Unix IPC")
        object.__setattr__(self, "comfy_root", str(Path(self.comfy_root).expanduser().absolute()))
        object.__setattr__(self, "workspace", str(Path(self.workspace).expanduser().absolute()))
        object.__setattr__(self, "comfy_args", tuple(self.comfy_args))
        for name in ("startup_timeout", "offline_grace", "transfer_idle_timeout"):
            if not isinstance(getattr(self, name), (int, float)) or not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(name + " must be positive and finite")
        for name in ("max_sessions", "max_call_records", "max_objects", "transfer_limit", "transfer_owner_limit"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name + " must be a positive integer")
        if self.max_file_bytes is not None and (type(self.max_file_bytes) is not int or self.max_file_bytes < 0):
            raise ValueError("max_file_bytes must be a nonnegative integer or None")


class ObservedPaths:
    """The last native path configuration, without importing ComfyUI or Torch."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.filename_list_cache = {}
        self.folder_names_and_paths = {name: (paths, set())
                                      for name, paths in snapshot["categories"].items()}

    def __getattr__(self, name):
        for directory in ("input", "temp", "output"):
            if name == f"get_{directory}_directory":
                return lambda directory=directory: self.snapshot["directories"][directory]
        raise AttributeError(name)

    def map_legacy(self, category):
        return self.snapshot.get("aliases", {}).get(category, category)

    def get_folder_paths(self, category):
        if category == "custom_nodes":
            if "custom_nodes" not in self.snapshot:
                raise ValueError("Native custom-node paths were not observed")
            return self.snapshot["custom_nodes"]
        if category not in self.snapshot["categories"]:
            raise ValueError("Unknown native model category")
        return self.snapshot["categories"][category]

    def get_full_path(self, category, name):
        for root in self.get_folder_paths(category):
            path = Path(root) / name
            if path.is_file():
                return str(path)
        return None


class RuntimeSupervisor:
    def __init__(self, config, *, config_path=None):
        self.config = config
        self.config_path = Path(config_path) if config_path else None
        self.maintenance_lock = self.mutation_lock = asyncio.Lock()
        self.process = None
        self.endpoint = None
        self.runtime_epoch = None
        self._node_metadata = None
        self.state = "stopped"
        self.error = None
        self.logs = deque(maxlen=256)
        self._token = secrets.token_urlsafe(32)
        self._client = None
        self._compute_transport = None
        self._compute_directory = None
        self._monitor = self._reader = None
        self.file_store = None
        self.roots_generation = None
        self._snapshot = None
        self._snapshot_signature = None
        self._closing = False
        self._bridge_directory = None
        self.runtime_bridge_digest = None
        self.control_barrier = None
        self._start_tasks = set()

    async def _http(self):
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(
                headers={"Authorization": "Bearer " + self._token},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                auto_decompress=False,
                connector=aiohttp.TCPConnector(limit=0))
        return self._client

    def assert_stopped(self):
        if self.process is not None and self.process.returncode is None:
            raise SupervisorError("RuntimeBusy", "Stop the runtime explicitly before modifying its environment")

    async def info(self):
        return {**asdict(self.config), "config": asdict(self.config),
                "state": self.state, "pid": self.process.pid if self.process and self.process.returncode is None else None,
                "runtime_epoch": self.runtime_epoch, "installed": (Path(self.config.comfy_root) / "execution.py").is_file(),
                "error": self.error, "runtime_bridge_digest": self.runtime_bridge_digest, "roots_generation": self.roots_generation,
                "roots_state": "live" if self.state == "running" else "last-observed" if self._snapshot else "unknown",
                "node_metadata": self.observed_nodes()}

    def observed_nodes(self):
        if (self.state == "running" and self._node_metadata is not None
                and self._node_metadata["epoch"] == self.runtime_epoch):
            return self._node_metadata
        return None

    async def configure(self, **fields):
        async with self.maintenance_lock:
            self.assert_stopped()
            unknown = set(fields) - set(RuntimeConfig.__dataclass_fields__)
            if unknown:
                raise ValueError("Unknown runtime configuration field: " + ", ".join(sorted(unknown)))
            selected = replace(self.config, **fields)
            paths_changed = any(getattr(selected, name) != getattr(self.config, name)
                                for name in ("comfy_root", "comfy_args"))
            self.config = selected
            if self.file_store is not None:
                with self.file_store._lock:
                    self.file_store.max_file_size = selected.max_file_bytes
                    self.file_store.transfer_idle_timeout = selected.transfer_idle_timeout
                    self.file_store.transfer_limit = selected.transfer_limit
                    self.file_store.transfer_owner_limit = selected.transfer_owner_limit
            if paths_changed:
                if self.file_store is not None:
                    await _io(self.file_store.close)
                self.file_store = self._snapshot = self.roots_generation = None
            if self.config_path:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
                temporary.write_text(json.dumps(asdict(self.config), indent=2))
                temporary.replace(self.config_path)
            return await self.info()

    async def runtime_rpc(self, method, params=None):
        if self.config.compute_transport == "zmq":
            return await self.runtime_request(method, **(params or {}))
        if self.endpoint is None or self.process is None or self.process.returncode is not None:
            raise SupervisorError("RuntimeStopped", "The runtime is stopped; call comfyui.start() explicitly")
        client = await self._http()
        async with client.post(self.endpoint + "/rpc", json={"method": method, "params": params or {}}) as response:
            payload = await response.json()
            if "error" in payload:
                error = payload["error"]
                raise SupervisorError(error.get("code", "RuntimeError"), error.get("message", "Runtime failed"))
            response.raise_for_status()
            return payload.get("result", payload)

    async def runtime_request(self, method, **params):
        if self.config.compute_transport == "zmq":
            if self._compute_transport is None or self.process is None or self.process.returncode is not None:
                raise SupervisorError("RuntimeStopped", "The runtime is stopped; call comfyui.start() explicitly")
            return await self._compute_transport.request(method, **params)
        if method == "fetch":
            from comfier.protocol import PICKLE_CONTENT_TYPE

            client = await self._http()
            if self.endpoint is None or self.process is None or self.process.returncode is not None:
                raise SupervisorError("RuntimeStopped", "The runtime is stopped; call comfyui.start() explicitly")
            async with client.post(self.endpoint + "/rpc", json={"method": method, "params": params},
                                   headers={"Accept": PICKLE_CONTENT_TYPE}, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.content_type == PICKLE_CONTENT_TYPE:
                    response.raise_for_status()
                    return await response.read()
                payload = await response.json()
                if "error" in payload:
                    from comfier.errors import error_from_wire
                    raise error_from_wire(payload["error"])
                response.raise_for_status()
                raise SupervisorError("InvalidResponse", "Runtime did not return a binary fetch response")
        return await self.runtime_rpc(method, params)

    async def refresh_roots(self):
        if self.state != "running":
            if self.file_store is not None and self._snapshot_signature == self._paths_signature():
                return self.file_store
            if not (Path(self.config.comfy_root) / "folder_paths.py").is_file():
                raise SupervisorError("RootsUnavailable", "ComfyUI is not installed; configure or install it before selecting native file roots")
            snapshot = await self._discover_paths()
        else:
            info = await self.runtime_rpc("runtime_info")
            snapshot = info["roots"]
        generation = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        if generation != self.roots_generation:
            paths = ObservedPaths(snapshot)
            if self.file_store is None:
                self.file_store = FileStore(paths, max_file_size=self.config.max_file_bytes,
                                            chunk_size=FILE_CHUNK_SIZE,
                                            transfer_idle_timeout=self.config.transfer_idle_timeout,
                                            transfer_limit=self.config.transfer_limit,
                                            transfer_owner_limit=self.config.transfer_owner_limit)
            else:
                self.file_store.update_paths(paths)
            self._snapshot, self.roots_generation = snapshot, generation
        self._snapshot_signature = self._paths_signature()
        return self.file_store

    def _paths_signature(self):
        root = Path(self.config.comfy_root)
        paths = [root / name for name in ("folder_paths.py", "main.py", "extra_model_paths.yaml")]
        collecting = False
        for argument in self.config.comfy_args:
            if argument.startswith("--extra-model-paths-config="):
                paths.append(Path(argument.split("=", 1)[1]))
            elif argument == "--extra-model-paths-config":
                collecting = True
            elif argument.startswith("-"):
                collecting = False
            elif collecting:
                paths.append(Path(argument))
        digest = hashlib.sha256()
        for path in paths:
            if not path.is_absolute():
                path = Path(self.config.workspace) / path
            digest.update(str(path).encode())
            try:
                digest.update(path.read_bytes())
            except FileNotFoundError:
                digest.update(b"missing")
        return digest.hexdigest()

    async def invalidate_roots(self):
        self._snapshot_signature = None
        # Keep stable IDs for unchanged native roots. The next query re-reads
        # configuration and invalidates removed roots via FileStore._root().

    async def files_changed(self, category=None):
        if category is not None and self.state == "running":
            await self.runtime_rpc("file_invalidate", {"category": category})

    def _bridge_path(self):
        if self._bridge_directory is None:
            self._bridge_directory = tempfile.TemporaryDirectory(prefix="comfier-runtime-bridge-")
            package = Path(__file__).resolve().parents[1]
            target = Path(self._bridge_directory.name) / "comfier"
            shutil.copytree(package, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            digest = hashlib.sha256()
            for path in sorted(target.rglob("*.py")):
                digest.update(path.relative_to(target).as_posix().encode())
                digest.update(path.read_bytes())
            self.runtime_bridge_digest = digest.hexdigest()
        return self._bridge_directory.name

    async def _discover_paths(self):
        env = {key: value for key, value in os.environ.items()
               if key not in {"COMFIER_TOKEN", "COMFIER_RUNTIME_TOKEN", "COMFIER_ZMQ_CURVE_SECRET_KEY",
                               "COMANI_TOKEN", "COMANI_RUNTIME_TOKEN", "COMANI_ZMQ_CURVE_SECRET_KEY"}}
        env["PYTHONPATH"] = self._bridge_path()
        process = await asyncio.create_subprocess_exec(
            self.config.runtime_python, "-c",
            "import json,sys;from comfier.daemon._paths import snapshot;json.dump(snapshot(json.load(sys.stdin)),sys.stdout)",
            cwd=self.config.workspace,
            env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(json.dumps(asdict(self.config)).encode()), 20)
            if process.returncode or len(output) > 4 * 1024**2:
                raise SupervisorError("RootsUnavailable", "The native path-only helper failed; start the runtime to discover supported roots")
            return json.loads(output)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    def file_limits(self):
        return {"max_file_bytes": self.config.max_file_bytes, "chunk_bytes": FILE_CHUNK_SIZE,
                "transfer_idle_timeout": self.config.transfer_idle_timeout,
                "transfer_limit": self.config.transfer_limit,
                "transfer_owner_limit": self.config.transfer_owner_limit}

    async def get_file_store(self):
        return await self.refresh_roots()

    async def file_roots(self, *, directory=None, category=None):
        from .file_http import root_wire

        store = await self.get_file_store()
        roots = await _io(store.roots, directory=directory, category=category)
        return [root_wire(store, root) for root in roots]

    async def start(self):
        task = asyncio.current_task()
        self._start_tasks.add(task)
        try:
            async with self.maintenance_lock:
                return await self._start()
        finally:
            self._start_tasks.discard(task)

    async def _validate_environment(self):
        code = """import importlib.util,json,sys
names=json.loads(sys.argv[1])
missing=[name for name in names if importlib.util.find_spec(name) is None]
print(json.dumps({'missing':missing,'python':list(sys.version_info[:3])}))
"""
        process = await asyncio.create_subprocess_exec(
            self.config.runtime_python, "-I", "-c", code,
            json.dumps(["aiohttp", "cloudpickle"] + (["zmq"] if self.config.compute_transport == "zmq" else [])),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 20)
            if process.returncode:
                raise SupervisorError("InvalidRuntimePython", "The selected runtime interpreter could not execute its environment check")
            info = json.loads(output)
            if info["python"] < [3, 11, 0] or info["missing"]:
                requirements = "Python 3.11+, aiohttp and cloudpickle" + (" and the comfier[zmq] extra" if self.config.compute_transport == "zmq" else "")
                raise SupervisorError("RuntimeEnvironmentIncomplete", "The selected runtime Python needs " + requirements + "; prepare it explicitly with comfyui.prepare_runtime(package=...) before start")
            return info
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def _start(self):
        if self._closing:
            raise SupervisorError("DaemonClosing", "The management daemon is closing")
        if self.process is not None and self.process.returncode is None:
            if self.state == "running":
                return await self.info()
            raise SupervisorError("RuntimeBusy", "The runtime is already starting or stopping")
        self.state, self.error = "starting", None
        try:
            if not (Path(self.config.comfy_root) / "execution.py").is_file():
                raise SupervisorError("ComfyUINotInstalled", "ComfyUI is not installed at comfy_root; install it explicitly before start")
            await self._validate_environment()
        except BaseException as error:
            self.state, self.error = "failed", str(error)
            raise
        # Bind first and inherit the descriptor. No probe/close/rebind port race.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.set_inheritable(True)
        port = listener.getsockname()[1]
        env = {key: value for key, value in os.environ.items()
               if key not in {"COMFIER_TOKEN", "COMFIER_RUNTIME_TOKEN", "COMFIER_ZMQ_CURVE_SECRET_KEY",
                               "COMANI_TOKEN", "COMANI_RUNTIME_TOKEN", "COMANI_ZMQ_CURVE_SECRET_KEY"}}
        env["PYTHONPATH"] = self._bridge_path()
        env["COMFIER_RUNTIME_TOKEN"] = self._token
        args = [self.config.runtime_python, "-u", "-c",
                "import json,os,sys;from comfier.daemon._worker import run;"
                "payload=json.load(sys.stdin);run(payload['config'],socket_fd=payload['socket_fd'],"
                "port=payload['port'],token=os.environ.get('COMFIER_RUNTIME_TOKEN'),"
                "compute_endpoint=payload.get('compute_endpoint'))"]
        socket_fd = listener.fileno() if os.name == "posix" else None
        payload = {"config": asdict(self.config), "socket_fd": socket_fd, "port": port}
        options = {}
        if os.name == "posix":
            options = {"pass_fds": (listener.fileno(),), "start_new_session": True}
        else:
            listener.close()
        try:
            if self.config.compute_transport == "zmq":
                from comfier import ZMQTransport

                self._compute_directory = tempfile.TemporaryDirectory(prefix="comfier-compute-")
                os.chmod(self._compute_directory.name, 0o700)
                compute_endpoint = "ipc://" + str(Path(self._compute_directory.name) / "rpc.sock")
                payload["compute_endpoint"] = compute_endpoint
                self._compute_transport = ZMQTransport(compute_endpoint, token=self._token, timeout=60)
                await self._compute_transport.open()
            self.process = await asyncio.create_subprocess_exec(
                *args, cwd=self.config.workspace, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, **options)
            self.endpoint = f"http://127.0.0.1:{port}"
            self._reader = asyncio.create_task(self._read_logs(self.process))
            self._monitor = asyncio.create_task(self._watch(self.process))
            self.process.stdin.write(json.dumps(payload).encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
            deadline = asyncio.get_running_loop().time() + self.config.startup_timeout
            while asyncio.get_running_loop().time() < deadline:
                if self.process.returncode is not None:
                    raise SupervisorError("RuntimeStartFailed", "Native runtime exited during startup; inspect its logs")
                try:
                    info = await asyncio.wait_for(self.runtime_rpc("runtime_info", {"include_node_metadata": True}), timeout=1)
                    self.runtime_epoch = info["epoch"]
                    self._node_metadata = info.get("node_metadata")
                    self.state = "running"
                    await self.refresh_roots()
                    return await self.info()
                except (aiohttp.ClientError, TimeoutError, ConnectionError):
                    await asyncio.sleep(0.1)
            raise SupervisorError("RuntimeStartTimeout", "The native runtime did not become ready before startup_timeout")
        except BaseException as error:
            await self._terminate(interrupt=isinstance(error, asyncio.CancelledError))
            self.state, self.error = "failed", str(error)
            raise
        finally:
            listener.close()

    async def _read_logs(self, process):
        while chunk := await process.stdout.read(4096):
            # Bounded diagnostic chunks, independent of line length.
            self.logs.append(chunk.decode(errors="replace"))

    async def _watch(self, process):
        code = await process.wait()
        if process is self.process:
            if self.state == "stopping":
                self.state = "stopped"
            elif self.state != "stopped":
                self.state = "failed"
                self.error = f"Native runtime exited with status {code}"
            self.endpoint = self.runtime_epoch = None
            await self._close_compute()

    async def _close_compute(self):
        transport, directory = self._compute_transport, self._compute_directory
        self._compute_transport = self._compute_directory = None
        if transport is not None:
            await transport.close()
        if directory is not None:
            directory.cleanup()

    async def _terminate(self, *, interrupt=False):
        process = self.process
        if process is not None and process.returncode is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL if interrupt else signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.kill() if interrupt else process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                await process.wait()
        if self._reader:
            await self._reader
        self.endpoint = self.runtime_epoch = None
        await self._close_compute()

    @asynccontextmanager
    async def _control_scope(self, *, wait):
        if self.control_barrier is None:
            if not wait:
                await self._cancel_starts()
            yield []
        else:
            async with self.control_barrier(wait=wait) as interrupted:
                if not wait:
                    await self._cancel_starts()
                yield interrupted

    async def _cancel_starts(self):
        current = asyncio.current_task()
        tasks = [task for task in self._start_tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self, *, wait=True, force=False):
        self._control_options(wait, force)
        async with self._control_scope(wait=wait) as interrupted:
            target = await self._drain() if wait else self.process
            async with self.maintenance_lock:
                result = await self.info() if wait and target is not self.process else await self._stop(wait=wait, force=force)
                return {**result, "interrupted_operations": interrupted}

    async def _stop(self, *, wait=True, force=False):
        if self.process is None or self.process.returncode is not None:
            self.state = "stopped"
            return await self.info()
        self.state = "stopping"
        await self._terminate(interrupt=not wait)
        self.state = "stopped"
        return await self.info()

    @staticmethod
    def _control_options(wait, force):
        if type(wait) is not bool or type(force) is not bool:
            raise SupervisorError("InvalidArgument", "wait and force must be booleans")
        if wait and force:
            raise SupervisorError("InvalidArgument", "wait=True conflicts with force=True")

    async def _drain(self):
        target = self.process
        while target is self.process and target is not None and target.returncode is None:
            try:
                info = await self.runtime_rpc("runtime_info")
            except (aiohttp.ClientError, SupervisorError):
                if target is not self.process or target.returncode is not None:
                    return target
                raise
            if not info["active"] and not info["pending"]:
                break
            await asyncio.sleep(0.1)
        return target

    async def restart(self, *, wait=True, force=False):
        self._control_options(wait, force)
        async with self._control_scope(wait=wait) as interrupted:
            target = await self._drain() if wait else self.process
            async with self.maintenance_lock:
                if wait and target is not self.process:
                    return {**await self.info(), "interrupted_operations": interrupted}
                await self._stop(wait=wait, force=force)
                try:
                    result = await self._start()
                except Exception as error:
                    raise SupervisorError(getattr(error, "code", "RuntimeStartFailed"), str(error),
                        {"interrupted_operations": interrupted, "runtime_state": self.state}) from error
                return {**result, "interrupted_operations": interrupted}

    async def close(self):
        self._closing = True
        await self.stop(wait=False, force=True)
        if self.file_store:
            await _io(self.file_store.close)
        if self._client:
            await self._client.close()
        if self._bridge_directory:
            self._bridge_directory.cleanup()
            self._bridge_directory = None
