"""Deployment preparation is explicit, observable and outside constructors."""

import asyncio
import sys
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from comani.cli import daemon as launcher
from comani.daemon.management.operations import ManagementError
from comani.daemon.management_http import create_management_app
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor


@asynccontextmanager
async def managed(supervisor, **options):
    app = create_management_app(supervisor, **options)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]) + "/manage"
    try:
        async with aiohttp.ClientSession() as client:
            async def request(method="handshake", params=None):
                async with client.post(url, json={"method": method, "params": params or {}}) as response:
                    payload = await response.json()
                    assert response.status == 200, payload
                    return payload["result"]
            yield app, request
    finally:
        await runner.cleanup()


def supervisor_at(tmp_path):
    return RuntimeSupervisor(RuntimeConfig(comfy_root=str(tmp_path / "ComfyUI"),
                                           runtime_python=sys.executable,
                                           workspace=str(tmp_path)))


async def deployment_settles(request):
    for _ in range(100):
        result = (await request())["download_tools"]["deployment"]
        if result and result["state"] != "preparing":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Deployment operation did not settle")


async def test_constructor_and_bare_app_do_not_prepare_tools(tmp_path, monkeypatch):
    from comani.daemon.management import download_tools

    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Bare management must not install system tools")

    monkeypatch.setattr(download_tools, "ensure_aria2", forbidden)
    supervisor = supervisor_at(tmp_path)
    app = create_management_app(supervisor)
    assert not calls
    await app["management"].close()
    await supervisor.close()
    async with managed(supervisor_at(tmp_path)) as (_, request):
        result = await request()
        assert result["download_tools"]["deployment"] is None
        assert result["download_tools"]["aria2"]["state"] == "not_checked"
    assert not calls


async def test_deployment_preparation_does_not_delay_auto_start(tmp_path, monkeypatch):
    from comani.daemon.management import download_tools

    entered, release = asyncio.Event(), asyncio.Event()
    calls, starts = [], []
    supervisor = supervisor_at(tmp_path)

    async def ensure(context, owner, *, cwd, timeout=None):
        calls.append((context.record.namespace, context.record.action, owner, cwd))
        entered.set()
        await release.wait()
        return {"binary": "/owned/aria2c", "version": "test", "installed": True, "installer": "fixture"}

    async def start():
        starts.append(True)
        supervisor.state = "running"

    monkeypatch.setattr(download_tools, "ensure_aria2", ensure)
    monkeypatch.setattr(supervisor, "start", start)
    async with managed(supervisor, prepare_download_tools=True, auto_start=True) as (app, request):
        await asyncio.wait_for(entered.wait(), 2)
        result = await request()
        assert result["download_tools"]["deployment"]["state"] == "preparing"
        assert starts == [True] and supervisor.state == "running"
        release.set()
        operation = await deployment_settles(request)
        assert operation["state"] == "ready" and starts == [True]
        assert calls == [("models", "prepare_download_tool", app["management"].providers, str(tmp_path))]


async def test_failed_preinstallation_keeps_repair_channel_and_failure_receipt(tmp_path, monkeypatch, caplog):
    from comani.daemon.management import download_tools

    supervisor = supervisor_at(tmp_path)
    starts = []

    async def ensure(*args, **kwargs):
        raise ManagementError("DownloadToolInstallationFailed", "fixture installer failed",
                              {"tool": "aria2", "phase": "install", "exit_code": 17})

    async def start():
        starts.append(True)
        supervisor.state = "running"

    monkeypatch.setattr(download_tools, "ensure_aria2", ensure)
    monkeypatch.setattr(supervisor, "start", start)
    async with managed(supervisor, prepare_download_tools=True, auto_start=True) as (_, request):
        deployment = await deployment_settles(request)
        assert deployment["state"] == "failed"
        assert deployment["error"]["code"] == "DownloadToolInstallationFailed"
        receipt = await request("management_status", {
            "operation_id": deployment["operation_id"], "daemon_epoch": deployment["daemon_epoch"]})
        assert receipt["state"] == "failed" and receipt["error"]["details"]["exit_code"] == 17
        info = await request("management_read", {"namespace": "comfyui", "action": "info"})
        assert info["state"] == "running"
        assert info["download_tools"]["deployment"]["state"] == "failed"
        assert starts == [True]
        assert "management remains available" in caplog.text


@pytest.mark.parametrize("no_start", [False, True])
def test_real_cli_enables_deployment_preparation(monkeypatch, tmp_path, no_start):
    calls = []
    monkeypatch.delenv("COMANI_TOKEN", raising=False)
    monkeypatch.setattr(launcher, "create_management_app",
                        lambda supervisor, **options: calls.append(options) or object())
    monkeypatch.setattr(launcher.web, "run_app", lambda app, **options: None)
    argv = ["--comfy-root", str(tmp_path / "ComfyUI")]
    launcher.main([*argv, *(["--no-start"] if no_start else [])])
    assert len(calls) == 1
    assert calls[0]["token"] is None
    assert calls[0]["auto_start"] is not no_start
    assert calls[0]["prepare_download_tools"] is True
