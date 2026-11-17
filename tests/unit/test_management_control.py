"""Control defaults, private plugin paths and interpreter metadata contracts."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfier.daemon.files import FileStore
from comfier.daemon.management import plugins
from comfier.daemon.management.models import ModelService
from comfier.daemon.management.operations import ManagementError
from comfier.daemon.management.processes import run_process
from comfier.daemon.management.service import ManagementService
from comfier.daemon.management.tools import python_metadata
from comfier.daemon.service import NativeRuntime
from comfier.daemon.supervisor import (
    ObservedPaths,
    RuntimeConfig,
    RuntimeSupervisor,
    SupervisorError,
)

COMFY = os.environ.get("COMFIER_COMFY_ROOT", "/home/delva/projects/ComfyUI")


async def test_emergency_stop_skips_compute_rpc_and_default_stop_waits_for_maintenance(tmp_path):
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path)))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Immediate stop must not wait on compute RPC")

    supervisor.runtime_rpc = forbidden
    supervisor.process = SimpleNamespace(returncode=None)
    interrupts = []

    async def terminate(*, interrupt=False):
        interrupts.append(interrupt)
        supervisor.process.returncode = -9

    supervisor._terminate = terminate
    assert (await supervisor.stop(wait=False))["state"] == "stopped"
    assert interrupts == [True]
    await supervisor.maintenance_lock.acquire()
    try:
        waiting = asyncio.create_task(supervisor.stop())
        await asyncio.sleep(0.02)
        assert not waiting.done()
    finally:
        supervisor.maintenance_lock.release()
    assert (await waiting)["state"] == "stopped"
    with pytest.raises(SupervisorError) as failure:
        await supervisor.restart(wait=True, force=True)
    assert failure.value.code == "InvalidArgument"


async def test_private_live_custom_node_roots_survive_observation_and_reject_ambiguity(tmp_path, monkeypatch):
    roots = [tmp_path / "default", tmp_path / "external"]
    for root in roots:
        plugin = root / "example-plugin"
        plugin.mkdir(parents=True)
        (plugin / "pyproject.toml").write_text('[project]\nname="example-plugin"\nversion="1.2.3"\n')
    native = SimpleNamespace(folder_names_and_paths={"custom_nodes": (roots, set()),
        "checkpoints": ([tmp_path / "models"], set())}, map_legacy=lambda category: category,
        get_folder_paths=lambda category: [str(value) for value in roots] if category == "custom_nodes" else [str(tmp_path / "models")])
    for directory in ("input", "output", "temp"):
        setattr(native, f"get_{directory}_directory", lambda directory=directory: str(tmp_path / directory))
    monkeypatch.setitem(sys.modules, "folder_paths", native)
    runtime = object.__new__(NativeRuntime)
    runtime.epoch, runtime.active, runtime.calls, runtime.session_seen = "test", None, {}, {}

    async def start():
        pass

    runtime.start = start
    snapshot = (await runtime.runtime_info())["roots"]
    assert snapshot["custom_nodes"] == [str(value) for value in roots]
    assert "custom_nodes" not in snapshot["categories"]
    store = FileStore(ObservedPaths(snapshot), max_file_size=None)
    supervisor = SimpleNamespace(config=RuntimeConfig(str(tmp_path), workspace=str(tmp_path)),
        maintenance_lock=asyncio.Lock(), assert_stopped=lambda: None)

    async def get_file_store():
        return store

    supervisor.get_file_store = get_file_store
    assert len(await plugins.listing(supervisor)) == 2
    with pytest.raises(ValueError):
        store.roots(category="custom_nodes")
    categories = await ModelService(SimpleNamespace(get_file_store=get_file_store,
        roots_generation="test", state="running"), None).categories()
    assert "custom_nodes" not in categories["categories"]
    context = SimpleNamespace(record=SimpleNamespace(), partial=lambda value: None,
                              progress=lambda **values: None)
    with pytest.raises(ManagementError) as failure:
        await plugins.change(supervisor, context, "disable", packages="example-plugin")
    assert failure.value.code == "AmbiguousPlugin"
    assert all((root / "example-plugin").exists() for root in roots)
    (roots[0] / "example-plugin" / "pyproject.toml").unlink()
    (roots[0] / "example-plugin").rmdir()
    changed = await plugins.change(supervisor, context, "disable", packages="example-plugin")
    assert changed["items"][0]["after"][0]["enabled"] is False
    assert Path(changed["items"][0]["after"][0]["path"]).is_relative_to(roots[1])
    assert (await plugins.change(supervisor, context, "enable", packages="example-plugin"))["items"][0]["after"][0]["enabled"] is True


@pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")
async def test_cold_path_helper_keeps_extra_custom_nodes_private_without_importing_plugins(tmp_path):
    extra = tmp_path / "external"
    plugin = extra / "example-plugin"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("raise AssertionError('path helper imported plugin code')\n")
    (plugin / "pyproject.toml").write_text('[project]\nname="example-plugin"\nversion="1.2.3"\n')
    config = tmp_path / "extra.yaml"
    config.write_text("external:\n  custom_nodes: " + json.dumps(str(extra)) + "\n")
    supervisor = RuntimeSupervisor(RuntimeConfig(COMFY, runtime_python=sys.executable,
        workspace=str(tmp_path), comfy_args=("--base-directory", str(tmp_path / "base"),
        "--extra-model-paths-config", str(config))))
    try:
        store = await supervisor.get_file_store()
        assert str(extra) in store.folder_paths.get_folder_paths("custom_nodes")
        assert "example-plugin" in {item["id"] for item in await plugins.listing(supervisor)}
        with pytest.raises(ValueError):
            await supervisor.file_roots(category="custom_nodes")
    finally:
        await supervisor.close()


async def test_interpreter_torch_fact_uses_distribution_metadata_without_import(tmp_path):
    import importlib.metadata

    try:
        expected = importlib.metadata.version("torch")
    except importlib.metadata.PackageNotFoundError:
        expected = None
    metadata = await python_metadata(sys.executable)
    assert metadata["packages"].get("torch", {}).get("version") == expected


async def admit(service, namespace, action, params, key):
    window = service.operations.window("test")
    value = await service.dispatch("management_begin", {"namespace": namespace, "action": action,
        "params": params, "key": key, "daemon_epoch": window["daemon_epoch"],
        "window": window["window"]}, "test")
    return service.operations.get(value["operation_id"], value["daemon_epoch"], "test")


async def marker(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.01)


def slow_maintenance(monkeypatch, *, cancelled=None, finish_cancel=None):
    from comfier.daemon.management import comfyui

    async def prepare(supervisor, context, *, package):
        async with supervisor.maintenance_lock:
            code = ("from pathlib import Path;import sys,time;prefix=Path(sys.argv[1]);"
                    "prefix.with_suffix('.changed').write_text('changed');"
                    "prefix.with_suffix('.entered').write_text('entered');"
                    "\nwhile not prefix.with_suffix('.release').exists():time.sleep(.01)\n")
            try:
                return await run_process(context, [sys.executable, "-c", code, package], cwd=supervisor.config.workspace)
            except asyncio.CancelledError:
                if cancelled is not None:
                    cancelled.set()
                    await finish_cancel.wait()
                raise

    monkeypatch.setattr(comfyui, "prepare_runtime", prepare)


async def test_default_stop_waits_for_related_maintenance_even_without_worker(tmp_path, monkeypatch):
    slow_maintenance(monkeypatch)
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path / "comfy"), workspace=str(tmp_path)))
    service = ManagementService(supervisor)
    try:
        prefix = tmp_path / "install"
        install = await admit(service, "comfyui", "prepare_runtime", {"package": str(prefix)}, "install")
        await marker(prefix.with_suffix(".entered"))
        stop = await admit(service, "comfyui", "stop", {}, "stop")
        await asyncio.sleep(0.05)
        assert not stop.task.done() and install.state == "running"
        prefix.with_suffix(".release").touch()
        await asyncio.wait_for(asyncio.gather(install.task, stop.task), 3)
        assert install.state == stop.state == "succeeded"
        assert stop.result["interrupted_operations"] == []
        assert prefix.with_suffix(".changed").read_text() == "changed"
    finally:
        await service.close()
        await supervisor.close()


async def test_emergency_cancels_related_running_and_queued_work_but_keeps_bash(tmp_path, monkeypatch):
    cancelled, finish_cancel = asyncio.Event(), asyncio.Event()
    slow_maintenance(monkeypatch, cancelled=cancelled, finish_cancel=finish_cancel)
    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path / "comfy"), workspace=str(tmp_path)))
    service = ManagementService(supervisor, operation_limits={"max_running": 2})
    try:
        shell = await admit(service, "bash", "run", {"script": "touch shell.entered; sleep 30"}, "shell")
        await marker(tmp_path / "shell.entered")
        prefix = tmp_path / "install"
        install = await admit(service, "comfyui", "prepare_runtime", {"package": str(prefix)}, "install")
        await marker(prefix.with_suffix(".entered"))
        queued_prefix = tmp_path / "queued"
        queued = await admit(service, "comfyui", "prepare_runtime", {"package": str(queued_prefix)}, "queued")
        start = await admit(service, "comfyui", "start", {}, "start")
        stop = await admit(service, "comfyui", "stop", {"wait": False}, "emergency")
        await asyncio.wait_for(cancelled.wait(), 3)
        with pytest.raises(ManagementError) as failure:
            await admit(service, "comfyui", "prepare_tools", {}, "new-maintenance")
        assert failure.value.code == "RuntimeControlBusy"
        finish_cancel.set()
        await asyncio.wait_for(stop.task, 3)
        assert stop.state == "succeeded"
        assert install.state == queued.state == start.state == "cancelled"
        assert install.result["partial_changes"] is True
        assert prefix.with_suffix(".changed").exists()
        assert not queued_prefix.with_suffix(".entered").exists()
        assert shell.state == "running"
        assert {item["operation_id"] for item in stop.result["interrupted_operations"]} == {install.id, queued.id, start.id}
        assert not supervisor.maintenance_lock.locked()
        await service.operations.cancel(shell)
    finally:
        finish_cancel.set()
        await service.close()
        await supervisor.close()
