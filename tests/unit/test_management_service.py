"""Real subprocess/HTTP checks for management lifetime and safe publication."""

import asyncio
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from comani.daemon.files import FileStore
from comani.daemon.management.operations import ManagementError, OperationRegistry
from comani.daemon.management.providers import ProviderResolver
from comani.daemon.management.service import ManagementService


class Paths:
    def __init__(self, path):
        self.path = path
        self.folder_names_and_paths = {"checkpoints": ([str(path)], set())}
        self.filename_list_cache = {}

    def get_folder_paths(self, category):
        if category == "custom_nodes":
            return [str(self.path.parent / "comfy" / "custom_nodes")]
        if category != "checkpoints":
            raise ValueError("Unknown native model category")
        return [str(self.path)]

    def get_full_path(self, category, name):
        path = self.path / name
        return str(path) if path.is_file() else None


class Supervisor:
    def __init__(self, path):
        self.config = SimpleNamespace(workspace=str(path), comfy_root=str(path / "comfy"),
                                      runtime_python=sys.executable)
        self.maintenance_lock = asyncio.Lock()
        self.roots_generation = "one"
        self.state = "stopped"
        self.file_store = FileStore(Paths(path / "models"), max_file_size=10 * 1024 * 1024)

    def assert_stopped(self):
        assert self.state == "stopped"

    async def get_file_store(self):
        return self.file_store


async def submit(service, namespace, action, params, key="key"):
    window = await service.dispatch("management_window", {}, "user")
    request = {"namespace": namespace, "action": action, "params": params, "key": key,
               "daemon_epoch": window["daemon_epoch"], "window": window["window"]}
    result = await service.dispatch("management_begin", request, "user")
    return request, result


async def finish(service, snapshot):
    rec = service.operations.get(snapshot["operation_id"], snapshot["daemon_epoch"], "user")
    await rec.task
    return service.operations.snapshot(rec)


async def test_admission_is_atomic_and_disconnect_independent(tmp_path):
    service = ManagementService(Supervisor(tmp_path))
    try:
        params = {"script": "sleep 0.05; printf ran >> runs", "check": True}
        request, original = await submit(service, "bash", "run", params)
        duplicates = await asyncio.gather(*(service.dispatch("management_begin", request, "user") for _ in range(30)))
        assert {item["operation_id"] for item in duplicates} == {original["operation_id"]}
        with pytest.raises(ManagementError, match="other arguments"):
            await service.dispatch("management_begin", {**request, "params": {"script": "touch extra"}}, "user")
        result = await finish(service, original)
        assert result["state"] == "succeeded"
        assert (tmp_path / "runs").read_text() == "ran"
        assert not (tmp_path / "extra").exists()
    finally:
        await service.close()


async def test_cancel_before_task_starts_has_terminal_fact(tmp_path):
    service = ManagementService(Supervisor(tmp_path))
    _, accepted = await submit(service, "bash", "run", {"script": "touch forbidden"})
    result = await service.dispatch("management_cancel", accepted, "user")
    assert result["state"] == "cancelled"
    assert not (tmp_path / "forbidden").exists()
    await service.close()


async def test_bash_timeout_cleans_descendant_with_exited_leader(tmp_path):
    service = ManagementService(Supervisor(tmp_path))
    _, accepted = await submit(service, "bash", "run", {"script": "sleep 60 & echo $! > child", "timeout": 0.1})
    result = await asyncio.wait_for(finish(service, accepted), 8)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "OperationTimeout"
    pid = (tmp_path / "child").read_text().strip()
    status = Path(f"/proc/{pid}/stat")
    assert not status.exists() or status.read_text().split()[2] == "Z"
    await service.close()


async def test_window_expiry_and_bounded_log_gap_are_explicit():
    registry = OperationRegistry(window_seconds=0.01, log_bytes=8)
    window = registry.window("user")
    await asyncio.sleep(0.02)
    with pytest.raises(ManagementError) as failure:
        await registry.begin("user", "bash", "run", {}, "key", registry.epoch, window["window"], lambda context: None)
    assert failure.value.code == "OperationExpired"
    registry.window_seconds = 300

    async def run(context):
        context.log("0123456789")
        context.log("tail")

    accepted = await registry.begin("user", "bash", "run", {}, "key", registry.epoch, registry.window("user")["window"], run)
    rec = registry.get(accepted["operation_id"], registry.epoch, "user")
    await rec.task
    with pytest.raises(ManagementError) as failure:
        await registry.logs(rec)
    assert failure.value.code == "LogExpired"
    assert (await registry.logs(rec, after=1))["entries"][0]["text"] == "tail"
    await registry.close()


async def test_known_submission_survives_expired_window_and_completed_are_not_active():
    registry = OperationRegistry(window_seconds=0.01, max_active=1, max_records=10)
    window = registry.window("user")
    runs = []

    async def run(context):
        runs.append(True)
        return {"fact": "retained"}

    accepted = await registry.begin("user", "bash", "run", {}, "same-key", registry.epoch, window["window"], run)
    rec = registry.get(accepted["operation_id"], registry.epoch, "user")
    await rec.task
    await asyncio.sleep(0.02)
    repeated = await registry.begin("user", "bash", "run", {}, "same-key", registry.epoch, window["window"], run)
    assert repeated["operation_id"] == accepted["operation_id"]
    assert repeated["result"] == {"fact": "retained"} and len(runs) == 1
    with pytest.raises(ManagementError) as failure:
        await registry.begin("user", "bash", "run", {}, "unknown-key", registry.epoch, window["window"], run)
    assert failure.value.code == "OperationExpired"
    next_window = registry.window("user")
    next_operation = await registry.begin("user", "bash", "run", {}, "next-key", registry.epoch, next_window["window"], run)
    await registry.get(next_operation["operation_id"], registry.epoch, "user").task
    assert len(runs) == 2
    await registry.close()


async def test_retained_result_quota_backpressures_new_work_but_keeps_known_fact():
    registry = OperationRegistry(max_result_bytes=1024, max_result_size=1024)
    window = registry.window("user")

    async def run(context):
        return "r" * 900

    accepted = await registry.begin("user", "bash", "run", {}, "key", registry.epoch, window["window"], run)
    await registry.get(accepted["operation_id"], registry.epoch, "user").task
    with pytest.raises(ManagementError) as failure:
        await registry.begin("user", "bash", "run", {}, "other", registry.epoch, window["window"], run)
    assert failure.value.code == "OperationCapacity"
    assert failure.value.details["resource"] == "results"
    attached = await registry.begin("user", "bash", "run", {}, "key", registry.epoch, window["window"], run)
    assert attached["result"] == "r" * 900
    await registry.close()


async def test_oversized_completed_result_is_a_retained_explicit_failure_not_reexecution():
    registry = OperationRegistry(max_result_bytes=4096, max_result_size=1024)
    window = registry.window("user")
    calls = []

    async def run(context):
        calls.append(True)
        return {"payload": "r" * 2048, "partial_changes": True}

    accepted = await registry.begin("user", "models", "download", {}, "key", registry.epoch, window["window"], run)
    record = registry.get(accepted["operation_id"], registry.epoch, "user")
    await record.task
    assert record.state == "failed" and record.error["code"] == "OperationResultTooLarge"
    assert record.result["completed_state"] == "succeeded"
    assert record.result["partial_changes"] is True
    repeated = await registry.begin("user", "models", "download", {}, "key", registry.epoch, window["window"], run)
    assert repeated["error"]["code"] == "OperationResultTooLarge" and len(calls) == 1
    assert registry._reserved_result_bytes == 0
    await registry.close()


@pytest.mark.parametrize("resume_status", [200, 206, 416])
async def test_provider_interruption_never_blindly_appends(tmp_path, resume_status):
    payload = b"artifact bytes" * 10000
    calls = []

    async def download(request):
        calls.append(request.headers.get("Range"))
        if len(calls) == 1:
            response = web.StreamResponse(headers={"Content-Length": str(len(payload)), "ETag": '"v1"'})
            await response.prepare(request)
            await response.write(payload[:10000])
            request.transport.close()
            return response
        offset = int(request.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
        if request.headers.get("Range") and resume_status == 416:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{len(payload)}"})
        if request.headers.get("Range") and resume_status == 206:
            return web.Response(status=206, body=payload[offset:], headers={"ETag": '"v1"', "Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}"})
        return web.Response(body=payload, headers={"ETag": '"v1"'})

    app = web.Application()
    app.router.add_get("/weights", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/weights"},
                "filename": "weights.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    try:
        _, accepted = await submit(service, "models", "download", {"artifact": artifact, "root": root.id, "transport": "http"})
        result = await asyncio.wait_for(finish(service, accepted), 10)
        assert result["state"] == "succeeded", result
        assert (root.path / "weights.bin").read_bytes() == payload
        assert calls[1] == "bytes=10000-"
        assert result["result"]["verified_against"] == "sha256"
    finally:
        await service.close()
        await runner.cleanup()


async def test_checksum_mismatch_does_not_publish(tmp_path):
    app = web.Application()

    async def corrupted(request):
        return web.Response(body=b"corrupted")

    app.router.add_get("/model", corrupted)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/model"},
                "filename": "weights.bin", "sha256": "0" * 64}
    try:
        _, accepted = await submit(service, "models", "download", {"artifact": artifact, "root": root.id})
        result = await finish(service, accepted)
        assert result["error"]["code"] == "ChecksumMismatch"
        assert not (root.path / "weights.bin").exists()
        staging = tmp_path / ".comani" / "staging"
        assert not staging.exists() or not list(staging.iterdir())
        # A disproven representation must not survive in the stable cache.
        cache = tmp_path / ".comani" / "model-resume"
        assert not [path for path in cache.iterdir() if path.is_dir()]
    finally:
        await service.close()
        await runner.cleanup()


async def test_existing_aria2_binary_streams_and_verifies_before_publish(tmp_path):
    if not shutil.which("aria2c"):
        pytest.skip("Optional aria2 binary is not installed")
    payload = bytes(range(256)) * 16384
    calls = []

    async def download(request):
        calls.append(request.headers.get("Range"))
        header = request.headers.get("Range")
        if not header:
            return web.Response(body=payload, headers={"Accept-Ranges": "bytes"})
        start, end = header.removeprefix("bytes=").split("-")
        start, end = int(start), int(end) if end else len(payload) - 1
        return web.Response(status=206, body=payload[start:end + 1], headers={
            "Content-Range": f"bytes {start}-{end}/{len(payload)}", "Accept-Ranges": "bytes"})

    app = web.Application()
    app.router.add_get("/model", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/model?token=must-not-log"},
                "filename": "weights.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    try:
        _, accepted = await submit(service, "models", "download", {"artifact": artifact, "root": root.id,
            "transport": "aria2", "connections": 4})
        result = await asyncio.wait_for(finish(service, accepted), 15)
        assert result["state"] == "succeeded", result
        assert result["result"]["transport"] == "aria2"
        assert (root.path / "weights.bin").read_bytes() == payload
        rec = service.operations.get(accepted["operation_id"], accepted["daemon_epoch"], "user")
        assert "must-not-log" not in str(list(rec.logs))
        assert calls
        staging = tmp_path / ".comani" / "staging"
        assert not staging.exists() or not list(staging.iterdir())
        # Successful create-only publication leaves the final model above,
        # while its SHA partial, control file and manifest are all released.
        cache = tmp_path / ".comani" / "model-resume"
        assert not [path for path in cache.iterdir() if path.is_dir()]
    finally:
        await service.close()
        await runner.cleanup()


@pytest.mark.parametrize("publish,corrupt", [("directory", False), ("directory", True), ("files", True)])
async def test_snapshot_atomic_directory_and_per_file_partial_facts(tmp_path, publish, corrupt):
    payload = {"unet/model.bin": b"unet content", "vae/model.bin": b"vae content"}
    visible_before_complete = []
    supervisor = Supervisor(tmp_path)
    root, = supervisor.file_store.roots(category="checkpoints")

    async def download(request):
        name = request.match_info["path"]
        if name.startswith("vae/"):
            visible_before_complete.append((root.path / "family").exists())
        return web.Response(body=payload[name])

    app = web.Application()
    app.router.add_get("/{path:.*}", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    service = ManagementService(supervisor)
    artifacts = [{"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/{name}"},
                  "relative_path": name, "filename": name.rsplit("/", 1)[-1], "size": len(data),
                  "sha256": "0" * 64 if corrupt and name.startswith("vae/") else hashlib.sha256(data).hexdigest()}
                 for name, data in payload.items()]
    try:
        _, accepted = await submit(service, "models", "snapshot", {"manifest": {"files": artifacts},
            "root": root.id, "path": "family", "publish": publish, "transport": "http"})
        result = await finish(service, accepted)
        if corrupt:
            assert result["state"] == "failed"
            assert result["result"]["complete"] is False
            assert (root.path / "family").exists() == (publish == "files")
            assert result["result"]["partial_changes"] == (publish == "files")
        else:
            assert result["state"] == "succeeded", result
            assert result["result"]["complete"] is True
            assert (root.path / "family/unet/model.bin").read_bytes() == payload["unet/model.bin"]
        assert visible_before_complete == [publish == "files"]
    finally:
        await service.close()
        await runner.cleanup()


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args], stderr=subprocess.DEVNULL).decode().strip()


async def test_explicit_git_plugin_install_and_update_pin_real_commits(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Management test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "node.py").write_text("VERSION = 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "first")
    first = git(repository, "rev-parse", "HEAD")
    service = ManagementService(Supervisor(tmp_path))
    try:
        _, accepted = await submit(service, "plugins", "install_git", {
            "repository": str(repository), "name": "tiny-plugin", "no_deps": True})
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert result["result"]["items"][0]["resolved_commit"] == first
        installed = tmp_path / "comfy/custom_nodes/tiny-plugin"
        assert git(installed, "rev-parse", "HEAD") == first
        (repository / "node.py").write_text("VERSION = 2\n")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "second")
        second = git(repository, "rev-parse", "HEAD")
        _, accepted = await submit(service, "plugins", "update_git", {
            "package": "tiny-plugin", "no_deps": True}, key="update")
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert result["result"]["items"][0]["resolved_commit"] == second
        assert git(installed, "rev-parse", "HEAD") == second
        (installed / "node.py").write_text("local modifications\n")
        _, accepted = await submit(service, "plugins", "update_git", {"package": "tiny-plugin", "commit": first}, key="dirty")
        result = await finish(service, accepted)
        assert result["error"]["code"] == "DirtyWorkingTree"
        assert (installed / "node.py").read_text() == "local modifications\n"
    finally:
        await service.close()


async def test_git_plugin_no_deps_still_reports_install_script_failure(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Management test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "install.py").write_text("raise SystemExit(7)\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "failing installer")
    service = ManagementService(Supervisor(tmp_path))
    try:
        _, accepted = await submit(service, "plugins", "install_git", {
            "repository": str(repository), "name": "failing-plugin", "no_deps": True})
        result = await finish(service, accepted)
        assert result["state"] == "failed"
        assert result["error"]["code"] == "PluginStepFailed"
        assert result["result"]["partial_changes"] is True
        assert (tmp_path / "comfy/custom_nodes/failing-plugin/install.py").exists()
    finally:
        await service.close()


async def test_hf_manifest_selection_fixes_whole_repo_and_subset_to_commit():
    resolver = ProviderResolver()
    seen = []

    async def metadata(url):
        seen.append(url)
        return {"sha": "a" * 40, "siblings": [
            {"rfilename": "unet/model.safetensors", "lfs": {"sha256": "b" * 64, "size": 100}},
            {"rfilename": "vae/config.json", "size": 20, "blobId": "c" * 40},
            {"rfilename": "README.md", "size": 10, "blobId": "d" * 40}]}

    resolver.json = metadata
    whole = await resolver.manifest({"repo_id": "owner/model", "revision": "main"})
    subset = await resolver.manifest({"repo_id": "owner/model"}, allow=["unet/*", "vae/*"], ignore=["*.md"])
    assert whole["revision"] == subset["revision"] == "a" * 40
    assert len(whole["files"]) == 3 and len(subset["files"]) == 2
    assert subset["total_size"] == 120
    assert subset["files"][1]["git_sha1"] == "c" * 40
    assert all(item["source"]["revision"] == "a" * 40 for item in whole["files"])
    assert all("blobs=true" in url for url in seen)


async def test_civitai_keeps_multiple_files_and_refreshes_selected_download_address():
    resolver = ProviderResolver()
    address = "https://cdn.example.invalid/model?token=first"

    async def metadata(url):
        return {"id": 7, "modelId": 2, "files": [
            {"id": 10, "name": "model-fp16.safetensors", "sizeKB": 1,
             "hashes": {"SHA256": "A" * 64}, "downloadUrl": address, "metadata": {"fp": "fp16"}},
            {"id": 11, "name": "model-fp32.safetensors", "sizeKB": 2,
             "hashes": {"SHA256": "B" * 64}, "downloadUrl": address, "metadata": {"fp": "fp32"}}]}

    resolver.json = metadata
    source = await resolver.resolve({"version_id": 7})
    assert len(source["files"]) == 2
    artifact = next(item for item in source["files"] if item["source"]["file_id"] == 10)
    assert "token=" not in str(artifact)
    assert await resolver.address(artifact) == address
    address = "https://cdn.example.invalid/model?token=renewed"
    assert await resolver.address(artifact) == address


async def test_auto_aria2_access_failure_is_explicit_without_http_fallback(tmp_path):
    if not shutil.which("aria2c"):
        pytest.skip("Optional aria2 binary is not installed")
    payload = b"verified model" * 10000
    agents = []

    async def download(request):
        agent = request.headers.get("User-Agent", "")
        agents.append(agent)
        if "aria2" in agent.lower():
            return web.Response(status=403)
        assert request.headers.get("Range") is None
        assert request.query["token"] == "signed-secret"
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/model", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/model?token=signed-secret"},
                "filename": "weights.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    try:
        _, accepted = await submit(service, "models", "download", {"artifact": artifact, "root": root.id})
        result = await asyncio.wait_for(finish(service, accepted), 10)
        assert result["state"] == "failed", result
        assert result["error"]["code"] == "ProviderTransferFailed"
        assert result["progress"]["transport"] == "aria2"
        assert "transport_fallback" not in result["progress"]
        assert "signed-secret" not in str(result)
        assert agents and all("aria2" in value.lower() for value in agents)
        assert not (root.path / "weights.bin").exists()
    finally:
        await service.close()
        await runner.cleanup()


async def test_create_environment_isolated_and_existing_directory_is_preserved(tmp_path):
    service = ManagementService(Supervisor(tmp_path))
    target = tmp_path / "new-environment"
    try:
        _, accepted = await submit(service, "comfyui", "create_environment", {"path": str(target)})
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert result["result"]["created"] is True
        assert result["result"]["environment"]["prefix"] == str(target)
        assert "include-system-site-packages = false" in (target / "pyvenv.cfg").read_text().lower()
        marker = target / "preserve"
        marker.write_text("existing user file")
        _, accepted = await submit(service, "comfyui", "create_environment", {"path": str(target)}, key="new-request")
        result = await finish(service, accepted)
        assert result["error"]["code"] == "FileExists"
        assert marker.read_text() == "existing user file"
    finally:
        await service.close()


async def test_core_update_accepts_exact_commit_and_reads_back_source(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Management test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "folder_paths.py").write_text("VERSION = 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "first")
    subprocess.run(["git", "clone", str(repository), str(tmp_path / "comfy")], check=True, capture_output=True)
    (repository / "folder_paths.py").write_text("VERSION = 2\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "second")
    selected = git(repository, "rev-parse", "HEAD")
    service = ManagementService(Supervisor(tmp_path))
    try:
        _, accepted = await submit(service, "comfyui", "update", {"commit": selected, "skip_requirements": True})
        result = await finish(service, accepted)
        assert result["state"] == "succeeded", result
        assert result["result"]["resolved_commit"] == selected
        assert git(tmp_path / "comfy", "rev-parse", "HEAD") == selected
        assert result["result"]["dependencies"] == "skipped"
    finally:
        await service.close()


async def test_registry_nightly_uses_explicit_git_source_without_packaged_manager(tmp_path, monkeypatch):
    from comani.daemon.management import plugins
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Management test")
    git(repository, "config", "user.email", "test@example.invalid")
    (repository / "node.py").write_text("VERSION = 1\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "nightly")

    async def registry(package):
        assert package == "tiny-registry"
        return str(repository)

    monkeypatch.setattr(plugins, "registry_repository", registry)
    service = ManagementService(Supervisor(tmp_path))
    try:
        _, accepted = await submit(service, "plugins", "install", {
            "packages": "tiny-registry", "version": "nightly", "no_deps": True})
        result = await asyncio.wait_for(finish(service, accepted), 10)
        assert result["state"] == "succeeded", result
        assert result["result"]["items"][0]["resolved_commit"] == git(repository, "rev-parse", "HEAD")
        assert (tmp_path / "comfy/custom_nodes/tiny-registry/node.py").exists()
    finally:
        await service.close()


async def test_model_reuse_hashes_existing_bytes_and_new_versions_can_coexist(tmp_path):
    payload = b"model bytes"
    requests = []

    async def download(request):
        requests.append(request.path)
        return web.Response(body=payload)

    app = web.Application()
    app.router.add_get("/model", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    supervisor = Supervisor(tmp_path)
    service = ManagementService(supervisor)
    root, = supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": f"http://127.0.0.1:{port}/model"},
                "filename": "model.bin", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    try:
        for version in ("v1", "v2"):
            _, accepted = await submit(service, "models", "download", {"artifact": artifact,
                "root": root.id, "path": version + "/model.bin", "transport": "http"}, key=version)
            assert (await finish(service, accepted))["state"] == "succeeded"
        _, accepted = await submit(service, "models", "download", {"artifact": artifact,
            "root": root.id, "path": "v1/model.bin", "reuse": True}, key="reuse")
        result = await finish(service, accepted)
        assert result["state"] == "succeeded" and result["result"]["reused"] is True
        assert len(requests) == 2
        (root.path / "v1/model.bin").write_bytes(b"wrong bytes")
        _, accepted = await submit(service, "models", "download", {"artifact": artifact,
            "root": root.id, "path": "v1/model.bin", "reuse": True}, key="changed")
        result = await finish(service, accepted)
        assert result["error"]["code"] == "ChecksumMismatch"
        assert (root.path / "v2/model.bin").read_bytes() == payload
        assert len(requests) == 2
    finally:
        await service.close()
        await runner.cleanup()
