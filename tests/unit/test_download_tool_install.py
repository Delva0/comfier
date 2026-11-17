"""Automatic aria2 preparation through owned executable package-manager fixtures."""

import asyncio
import hashlib
import json
import shutil
import sys

import pytest
from aiohttp import web

from comfier.daemon.management.download_tools import aria2_status
from comfier.daemon.management.service import ManagementService

from .test_management_service import Supervisor, finish, submit
from .test_model_download_transport import serve


@pytest.fixture
def installer(tmp_path, monkeypatch):
    binary = shutil.which("aria2c")
    if not binary:
        pytest.skip("Installation fixture copies a real test aria2 binary")
    directory, log = tmp_path / "bin", tmp_path / "installer.jsonl"
    directory.mkdir()
    script = f'''#!{sys.executable}
import json,os,pathlib,shutil,sys,time
args=sys.argv[1:]
with open({str(log)!r},"a") as stream: stream.write(json.dumps(args)+"\\n")
time.sleep(.08)
mode=os.environ.get("COMFIER_TEST_ARIA_MODE","success")
if mode=="failure": sys.exit(9)
if "install" in args and mode!="missing_output":
 target=pathlib.Path({str(directory / 'aria2c')!r})
 if mode=="broken_output": target.write_text({('#!' + sys.executable + chr(10) + 'print("not aria2")' + chr(10))!r})
 else: shutil.copyfile({binary!r},target)
 target.chmod(0o755)
'''
    (directory / "apt-get").write_text(script)
    (directory / "apt-get").chmod(0o755)
    sudo = f'''#!{sys.executable}
import os,sys
args=sys.argv[1:]
while args and args[0] in ("-n","--"): args.pop(0)
if args and args[0]=="env":
 args.pop(0)
 while args and "=" in args[0]:
  name,value=args.pop(0).split("=",1);os.environ[name]=value
os.execv(args[0],args)
'''
    (directory / "sudo").write_text(sudo)
    (directory / "sudo").chmod(0o755)
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv("COMFIER_TEST_ARIA_MODE", "success")
    return directory, log, binary


def events(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


async def begin_download(service, root, url, payload, name, transport="auto"):
    artifact = {"provider": "url", "source": {"url": url}, "filename": name,
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    _, accepted = await submit(service, "models", "download", {
        "artifact": artifact, "root": root.id, "path": name, "transport": transport}, key=name)
    return accepted


@pytest.mark.parametrize("transport", ["auto", "aria2"])
async def test_missing_tool_is_installed_version_checked_and_actually_downloads(tmp_path, installer, transport):
    directory, log, _ = installer
    payload, agents = b"owned model" * 65536, []

    async def source(request):
        agents.append(request.headers.get("User-Agent", ""))
        return web.Response(body=payload)

    runner, url = await serve(source)
    service = ManagementService(Supervisor(tmp_path / "workspace"))
    root, = service.supervisor.file_store.roots(category="checkpoints")
    try:
        assert not log.exists() and not (directory / "aria2c").exists()
        accepted = await begin_download(service, root, url, payload, "model.bin", transport)
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert (root.path / "model.bin").read_bytes() == payload
        assert result["result"]["transport"] == "aria2"
        assert result["progress"]["download_tool"]["installed"] is True
        assert result["progress"]["download_tool"]["version"]
        assert aria2_status(service.providers)["state"] == "ready"
        assert sum("install" in event for event in events(log)) == 1
        assert agents and all("aria2" in agent.lower() for agent in agents)
    finally:
        await service.close()
        await runner.cleanup()


@pytest.mark.parametrize("failure", [False, True])
async def test_concurrent_first_downloads_share_one_install_attempt(tmp_path, installer, monkeypatch, failure):
    _, log, _ = installer
    if failure:
        monkeypatch.setenv("COMFIER_TEST_ARIA_MODE", "failure")
    payload, requests = b"owned model" * 65536, []

    async def source(request):
        requests.append(request.method)
        return web.Response(body=payload)

    runner, url = await serve(source)
    service = ManagementService(Supervisor(tmp_path / "workspace"))
    root, = service.supervisor.file_store.roots(category="checkpoints")
    try:
        handles = await asyncio.gather(*(begin_download(service, root, url, payload, f"{index}.bin")
                                         for index in range(2)))
        results = await asyncio.gather(*(finish(service, handle) for handle in handles))
        assert sum(event == ["update"] for event in events(log)) == 1
        assert sum("install" in event for event in events(log)) == (0 if failure else 1)
        if failure:
            assert all(result["state"] == "failed" for result in results)
            assert all(result["error"]["code"] == "DownloadToolInstallationFailed" for result in results)
            assert sum(bool(result["error"]["details"].get("coalesced")) for result in results) == 1
            assert not requests
        else:
            assert all(result["state"] == "succeeded" for result in results), results
            assert all((root.path / f"{index}.bin").read_bytes() == payload for index in range(2))
    finally:
        await service.close()
        await runner.cleanup()


@pytest.mark.parametrize("mode,phase", [("failure", "apt_update"), ("missing_output", "verify_installed"),
                                        ("broken_output", "verify_installed")])
async def test_failed_or_unusable_install_is_not_ready_and_a_new_call_can_retry(tmp_path, installer, monkeypatch, mode, phase):
    _, _, _ = installer
    monkeypatch.setenv("COMFIER_TEST_ARIA_MODE", mode)
    payload, requests = b"model bytes", []

    async def source(request):
        requests.append(request.method)
        return web.Response(body=payload)

    runner, url = await serve(source)
    service = ManagementService(Supervisor(tmp_path / "workspace"))
    root, = service.supervisor.file_store.roots(category="checkpoints")
    try:
        accepted = await begin_download(service, root, url, payload, "failed.bin")
        result = await finish(service, accepted)
        assert result["state"] == "failed" and result["error"]["code"] == "DownloadToolInstallationFailed"
        assert result["error"]["details"]["phase"] == phase
        assert aria2_status(service.providers)["state"] == "failed"
        assert not requests and not (root.path / "failed.bin").exists()
        monkeypatch.setenv("COMFIER_TEST_ARIA_MODE", "success")
        accepted = await begin_download(service, root, url, payload, "retry.bin")
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert aria2_status(service.providers)["state"] == "ready"
        assert (root.path / "retry.bin").read_bytes() == payload
    finally:
        await service.close()
        await runner.cleanup()
