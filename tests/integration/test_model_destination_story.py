"""Independent public-HTTP acceptance for native model destination selection.

The tiny plugin registers directories through ComfyUI's own folder_paths API.
Payloads and both HTTP listeners stay on loopback; no provider or model is loaded.
"""

import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web

import comani
from comani.daemon.management_http import create_management_app
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor

pytestmark = pytest.mark.native

PROJECT = Path(__file__).resolve().parents[2]
COMFY = Path(os.environ.get("COMANI_COMFY_ROOT", PROJECT.parent / "ComfyUI"))
RUNTIME_PYTHON = os.environ.get(
    "COMANI_NATIVE_PYTHON", sys.executable
)
CATEGORY = "acceptance_plugin_models"
PAYLOAD = bytes(range(256)) * 4097
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()

REGISTER = """def main(category, paths):
    import folder_paths
    folder_paths.folder_names_and_paths.pop(category, None)
    for path in paths:
        folder_paths.add_model_folder_path(category, path)
    return list(folder_paths.get_folder_paths(category))
"""


def write_report(name, report, tmp_path):
    directory = Path(os.environ.get("COMANI_MODEL_DESTINATION_REPORT_DIR", tmp_path))
    directory.mkdir(parents=True, exist_ok=True)
    report["source_sha256"] = {
        relative: hashlib.sha256((PROJECT / relative).read_bytes()).hexdigest()
        for relative in ("src/comani/models.py", "src/comani/management.py",
                         "src/comani/daemon/management/models.py", "docs/design/models.md")}
    report["runtime_python"] = RUNTIME_PYTHON
    report["comfy_root"] = str(COMFY)
    (directory / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")


class Story:
    def __init__(self, remote, base, first, second, builtin, requests, transfers):
        self.remote, self.base = remote, base
        self.first, self.second, self.builtin = first, second, builtin
        self.requests, self.transfers = requests, transfers
        self.gates = {}

    def artifact(self, name):
        return {"provider": "url", "source": {"url": self.base + "/" + name},
                "filename": name, "size": len(PAYLOAD), "sha256": DIGEST}

    async def register(self, paths, category=CATEGORY):
        async def apply():
            output = await self.remote.python(
                code=REGISTER, entry="main",
                inputs={"category": category, "paths": list(map(str, paths))})
            assert await output.fetch() == list(map(str, paths))

        await asyncio.wait_for(apply(), 15)

    def snapshot(self):
        return len(self.requests), len(self.transfers)

    def measurement(self, before):
        calls = self.requests[before[0]:]
        return {"rpc_count": len(calls), "rpc_methods": dict(Counter(calls)),
                "source_gets": len(self.transfers) - before[1]}

    def verify(self, file, root, name):
        assert Path(file.root.path) == root
        assert file.relative_path == name and file.sha256 == DIGEST
        assert (root / name).read_bytes() == PAYLOAD


@asynccontextmanager
async def native_story(tmp_path, *, start=True):
    if not (COMFY / "execution.py").is_file() or not Path(RUNTIME_PYTHON).is_file():
        pytest.skip("Native ComfyUI checkout and its existing Python environment required")
    base = tmp_path / "native"
    first, second = tmp_path / "plugin-first", tmp_path / "plugin-second"
    first.mkdir()
    second.mkdir()
    for directory in ("user", "input", "output", "temp"):
        (tmp_path / directory).mkdir()
    plugin = base / "custom_nodes/acceptance_model_paths"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text(
        "import folder_paths\n"
        f"folder_paths.add_model_folder_path({CATEGORY!r}, {str(first)!r})\n"
        f"folder_paths.add_model_folder_path({CATEGORY!r}, {str(second)!r})\n"
        "NODE_CLASS_MAPPINGS = {}\n")
    supervisor = RuntimeSupervisor(RuntimeConfig(
        comfy_root=str(COMFY), runtime_python=RUNTIME_PYTHON, workspace=str(tmp_path),
        cpu=True, custom_nodes=True, comfy_args=(
            "--disable-partner-nodes", "--base-directory", str(base),
            "--user-directory", str(tmp_path / "user"),
            "--input-directory", str(tmp_path / "input"),
            "--output-directory", str(tmp_path / "output"),
            "--temp-directory", str(tmp_path / "temp"),
            "--whitelist-custom-nodes", "acceptance_model_paths")))
    requests, transfers = [], []

    @web.middleware
    async def count_rpc(request, handler):
        # The proxy streams /rpc's original body. Reading it here would consume
        # that stream before forwarding, even though request.json caches JSON.
        if request.method == "POST" and request.path == "/manage":
            body = await request.json()
            method = body.get("method", "?")
            if method in {"management_query", "management_read", "management_begin"}:
                params = body.get("params", {})
                method += ":" + params.get("namespace", "?") + "." + params.get("action", "?")
            requests.append(method)
        elif request.method == "POST" and request.path == "/rpc":
            requests.append("compute_proxy")
        return await handler(request)

    app = create_management_app(supervisor)
    app.middlewares.insert(0, count_rpc)
    daemon = web.AppRunner(app)
    source = web.Application()
    story = None

    async def payload(request):
        name = request.match_info["name"]
        transfers.append(name)
        started, release = story.gates.get(name, (None, None))
        if started is not None:
            started.set()
            await release.wait()
        return web.Response(body=PAYLOAD)

    source.router.add_get("/{name}", payload)
    server = web.AppRunner(source)
    try:
        await daemon.setup()
        site = web.TCPSite(daemon, "127.0.0.1", 0)
        await site.start()
        url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        source_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        async with comani.Client(url) as remote:
            if start:
                try:
                    await remote.comfyui.start()
                except comani.OperationError as error:
                    raise AssertionError({"startup": error.details,
                                          "logs": await remote.comfyui.logs()}) from error
                roots = await remote.files.roots(category=CATEGORY)
                assert [Path(root.path) for root in roots] == [first, second]
            builtin_roots = await remote.files.roots(category="checkpoints")
            builtin = base / "models/checkpoints"
            assert Path(builtin_roots[0].path) == builtin
            story = Story(remote, source_url, first, second, builtin, requests, transfers)
            try:
                yield story
            finally:
                for _, release in story.gates.values():
                    release.set()
    finally:
        if story is not None:
            for _, release in story.gates.values():
                release.set()
        await daemon.cleanup()
        await server.cleanup()


async def legacy_example_prepare(story, category, name):
    """The old Score/Qwen example story, measured rather than optimized here."""
    roots = await story.remote.files.roots(category=category)
    listing = await story.remote.models.list(category=category, prefix=name)
    present = {entry["root"]["id"] for entry in listing["files"]
               if entry["relative_path"] == name}
    root = min([value for value in roots if value.id in present] or roots,
               key=lambda value: value.priority)
    return await story.remote.models.download(
        story.artifact(name), root=root, path=name, reuse=True, transport="http")


async def test_native_model_root_baseline(tmp_path):
    report = {"scope": "existing examples' explicit-root story; real CPU native daemon",
              "payload_bytes": len(PAYLOAD), "payload_sha256": DIGEST, "cases": {}}
    try:
        async with native_story(tmp_path) as story:
            before = story.snapshot()
            source = await comani.models.resolve(story.base + "/builtin.bin")
            assert len(source.files) == 1
            assert story.snapshot() == before  # URL resolve is local metadata, not a daemon RPC.
            file = await legacy_example_prepare(story, "checkpoints", "builtin.bin")
            story.verify(file, story.builtin, "builtin.bin")
            report["cases"]["builtin_missing"] = story.measurement(before)

            (story.second / "reuse.bin").write_bytes(PAYLOAD)
            before = story.snapshot()
            file = await legacy_example_prepare(story, CATEGORY, "reuse.bin")
            story.verify(file, story.second, "reuse.bin")
            assert not (story.first / "reuse.bin").exists()
            report["cases"]["plugin_later_root_reuse"] = story.measurement(before)
            assert report["cases"]["plugin_later_root_reuse"]["source_gets"] == 0

            second = (await story.remote.files.roots(category=CATEGORY))[1]
            before = story.snapshot()
            file = await story.remote.models.download(
                story.artifact("explicit.bin"), root=second, path="explicit.bin", transport="http")
            story.verify(file, story.second, "explicit.bin")
            assert not (story.first / "explicit.bin").exists()
            report["cases"]["explicit_registered_directory"] = story.measurement(before)
            report["passed"] = True
    finally:
        write_report("baseline", report, tmp_path)


async def test_native_model_category_destination_story(tmp_path):
    report = {"scope": "category destination candidate; real CPU native daemon and plugin",
              "payload_bytes": len(PAYLOAD), "payload_sha256": DIGEST, "cases": {}}
    try:
        async with native_story(tmp_path) as story:
            before = story.snapshot()
            file = await story.remote.models.download(
                story.artifact("builtin.bin"), category="checkpoints", path="builtin.bin",
                reuse=True, transport="http")
            story.verify(file, story.builtin, "builtin.bin")
            report["cases"]["builtin_missing"] = story.measurement(before)

            (story.second / "reuse.bin").write_bytes(PAYLOAD)
            before = story.snapshot()
            file = await story.remote.models.download(
                story.artifact("reuse.bin"), category=CATEGORY, path="reuse.bin",
                reuse=True, transport="http")
            story.verify(file, story.second, "reuse.bin")
            assert not (story.first / "reuse.bin").exists()
            report["cases"]["plugin_later_root_reuse"] = story.measurement(before)
            assert report["cases"]["plugin_later_root_reuse"]["source_gets"] == 0

            (story.first / "shadow.bin").write_bytes(b"different visible model")
            (story.second / "shadow.bin").write_bytes(PAYLOAD)
            before = story.snapshot()
            with pytest.raises(comani.OperationError) as failure:
                await story.remote.models.download(
                    story.artifact("shadow.bin"), category=CATEGORY, path="shadow.bin",
                    reuse=True, transport="http")
            assert failure.value.details["error"]["code"] == "ChecksumMismatch"
            assert (story.first / "shadow.bin").read_bytes() == b"different visible model"
            report["cases"]["visible_shadow_rejected"] = story.measurement(before)
            assert report["cases"]["visible_shadow_rejected"]["source_gets"] == 0

            second = (await story.remote.files.roots(category=CATEGORY))[1]
            file = await story.remote.models.download(
                story.artifact("explicit.bin"), root=second, path="explicit.bin", transport="http")
            story.verify(file, story.second, "explicit.bin")
            assert not (story.first / "explicit.bin").exists()

            # A changed registry must affect the next category selection.
            await story.register([story.second, story.first])
            file = await story.remote.models.download(
                story.artifact("after-reorder.bin"), category=CATEGORY,
                path="after-reorder.bin", transport="http")
            story.verify(file, story.second, "after-reorder.bin")
            assert not (story.first / "after-reorder.bin").exists()
            report["cases"]["registration_reordered_before_selection"] = {"correct_root": True}

            # Once accepted, ordering changes must not silently redirect a transfer.
            started, release = asyncio.Event(), asyncio.Event()
            story.gates["in-flight.bin"] = (started, release)
            operation = await story.remote.models.begin_download(
                story.artifact("in-flight.bin"), category=CATEGORY,
                path="in-flight.bin", transport="http")
            await asyncio.wait_for(started.wait(), 10)
            await story.register([story.first, story.second])
            release.set()
            file = await asyncio.wait_for(operation.result(), 15)
            story.verify(file, story.second, "in-flight.bin")
            assert not (story.first / "in-flight.bin").exists()
            report["cases"]["registration_reordered_during_transfer"] = {"accepted_root_kept": True}

            started, release = asyncio.Event(), asyncio.Event()
            story.gates["removed.bin"] = (started, release)
            operation = await story.remote.models.begin_download(
                story.artifact("removed.bin"), category=CATEGORY,
                path="removed.bin", transport="http")
            await asyncio.wait_for(started.wait(), 10)
            await story.register([story.second])
            release.set()
            with pytest.raises(comani.OperationError) as failure:
                await asyncio.wait_for(operation.result(), 15)
            assert failure.value.details["error"]["code"] == "RootsChanged"
            assert not (story.first / "removed.bin").exists()
            assert not (story.second / "removed.bin").exists()
            report["cases"]["accepted_root_removed_during_transfer"] = {
                "code": failure.value.details["error"]["code"]}

            # Invalid/ambiguous destinations must not start provider traffic.
            for selection in ({}, {"root": second, "category": CATEGORY},
                              {"category": "not_registered_for_acceptance"}):
                before_gets = len(story.transfers)
                with pytest.raises((ValueError, TypeError, comani.OperationError)):
                    await story.remote.models.download(
                        story.artifact("invalid.bin"), path="invalid.bin", **selection)
                assert len(story.transfers) == before_gets
            report["cases"]["invalid_destinations"] = {"rejected_without_source_get": True}

            await story.remote.comfyui.stop()
            before = story.snapshot()
            file = await story.remote.models.download(
                story.artifact("stopped.bin"), category=CATEGORY, path="stopped.bin",
                transport="http")
            story.verify(file, story.second, "stopped.bin")
            info = await story.remote.comfyui.info()
            assert info.state == "stopped" and info.pid is None
            report["cases"]["stopped_last_observed_plugin_category"] = {
                **story.measurement(before), "did_not_start_native_worker": True}
            report["passed"] = True
    finally:
        write_report("candidate", report, tmp_path)




async def test_cold_model_category_does_not_start_or_import_plugins(tmp_path):
    report = {"scope": "cold native path-only configuration, no worker or plugin execution"}
    try:
        async with native_story(tmp_path, start=False) as story:
            file = await story.remote.models.download(
                story.artifact("cold.bin"), category="checkpoints", transport="http")
            story.verify(file, story.builtin, "cold.bin")
            before_gets = len(story.transfers)
            with pytest.raises(comani.OperationError) as failure:
                await story.remote.models.download(
                    story.artifact("unregistered.bin"), category=CATEGORY, transport="http")
            assert failure.value.details["error"]["code"] == "InvalidArgument"
            assert len(story.transfers) == before_gets
            info = await story.remote.comfyui.info()
            assert info.state == "stopped" and info.pid is None
            report.update({"builtin_published": True, "unknown_plugin_category_rejected": True,
                           "native_worker_not_started": True, "passed": True})
    finally:
        write_report("cold-category", report, tmp_path)


async def test_native_model_category_legacy_aliases(tmp_path):
    report = {"scope": "native unet/clip category aliases, no model loading", "cases": {}}
    try:
        async with native_story(tmp_path) as story:
            for alias, canonical in (("unet", "diffusion_models"), ("clip", "text_encoders")):
                roots = await story.remote.files.roots(category=canonical)
                file = await story.remote.models.download(
                    story.artifact(alias + ".bin"), category=alias, transport="http")
                assert file.root.category == canonical and file.root.id == roots[0].id
                story.verify(file, Path(roots[0].path), alias + ".bin")
                report["cases"][alias] = {"canonical_category": file.root.category,
                                          "root": str(file.root.path), "published": True}
            report["passed"] = True
    finally:
        write_report("native-aliases", report, tmp_path)
