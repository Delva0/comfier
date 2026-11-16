"""Actual native SaveImage -> failure preserves UI facts and original failure."""

import os
from pathlib import Path

import pytest

from comani import CallExpired, Client, Expired, InProcessTransport, RemoteError, SessionClosed
from comani.daemon import NativeRuntime

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not Path(COMFY, "execution.py").is_file(), reason="real ComfyUI required")


async def test_native_partial_ui_failure_observation_and_cleanup(tmp_path):
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    import folder_paths

    original_output = folder_paths.get_output_directory()
    folder_paths.set_output_directory(str(tmp_path))
    session = None
    try:
        async with Client(transport=InProcessTransport(runtime)) as remote:
            session = remote.session_id
            with remote.graph() as graph:
                image = remote.node("EmptyImage", width=8, height=8, batch_size=1, color=0x223344)
                saved = remote.node("SaveImage", images=image, filename_prefix="partial/committed", return_call=True)
                failure = remote.python(code="def fail(image):\n    raise RuntimeError('later branch failed')\n",
                                        entry="fail", inputs={"image": image})
                remote.node("SaveImage", images=failure, filename_prefix="partial/never")
                graph.output("image", image)
            run = remote.submit(graph, return_call=True)
            early = run[image]
            # This pre-bound consumer may use a completed node even if its graph
            # later fails. Observation must not change that existing behavior.
            consumer = remote.python(lambda value: tuple(value.shape), inputs={"value": early})
            for method in (lambda: run, run.metadata, run.files, run.output):
                with pytest.raises(RemoteError, match="later branch failed"):
                    await method()
            assert await (await consumer).fetch() == (1, 8, 8, 3)
            await runtime.queue.join()
            leases_before = {key: entry.leases for key, entry in runtime.store.entries.items()}
            observed = await run.metadata(allow_failed=True)
            assert observed.status == "failed" and "later branch failed" in observed.error["message"]
            assert observed.call_id == run.id == observed.prompt_id
            assert set(observed.ui) == {saved.id}
            assert "prompt" not in observed.ui and "extra_data" not in observed.ui
            files = await run.files(allow_failed=True, node_id=saved.id)
            assert len(files) == 1 and files[0].relative_path.startswith("partial/committed_")
            assert (tmp_path / files[0].relative_path).is_file()
            assert await run.file(allow_failed=True) == files[0]
            assert {key: entry.leases for key, entry in runtime.store.entries.items()} == leases_before
            assert len(list(tmp_path.rglob("*.png"))) == 1
            # Borrowed output expiry remains independent from retained JSON UI.
            ref = await early
            assert ref.ownership == "borrowed"
            runtime.adapter.executor.caches.outputs.cache.clear()
            with pytest.raises(Expired):
                await ref.fetch()
            assert (await run.metadata(allow_failed=True)).ui == observed.ui
            await remote.files.remove(files[0])
            with pytest.raises(RemoteError):
                await run.files(allow_failed=True)
            assert (await run.metadata(allow_failed=True)).ui == observed.ui
            # A validation failure after prior UI must never inherit old files.
            invalid = remote.submit({"bad": {"class_type": "SaveImage", "inputs": {}}}, return_call=True)
            invalid_metadata = await invalid.metadata(allow_failed=True)
            assert invalid_metadata.status == "failed" and invalid_metadata.error
            assert invalid_metadata.ui == {} and await invalid.files(allow_failed=True) == ()
            empty = remote.python(code="def fail():\n    raise RuntimeError('no products')\n", entry="fail", return_call=True)
            assert (await empty.metadata(allow_failed=True)).ui == {}
            assert await empty.files(allow_failed=True) == ()
            with pytest.raises(ValueError, match="found 0"):
                await empty.file(allow_failed=True)
            # Success continues to use the same metadata/files path.
            fresh = remote.node("EmptyImage", width=8, height=8, batch_size=1, color=0x778899)
            success = remote.node("SaveImage", images=fresh, filename_prefix="success", return_call=True)
            ready = await success
            assert len(ready) == len(runtime.adapter.nodes.NODE_CLASS_MAPPINGS["SaveImage"].RETURN_TYPES)
            assert all(ref.ownership == "owned" for ref in ready)
            success_metadata = await success.metadata()
            assert success_metadata.status == "succeeded" and success_metadata.error is None
            assert await success.metadata(allow_failed=True) == success_metadata
            assert (await success.file()).relative_path.startswith("success_")
            async with Client(transport=InProcessTransport(runtime)) as other:
                with pytest.raises(CallExpired):
                    await other._recovery.request("wait_result", session_id=other.session_id,
                                                  call_id=run.id, observation=True)
            await runtime.forget_calls(session, [run.id], runtime.epoch)
            with pytest.raises(CallExpired):
                await run.metadata(allow_failed=True)
        assert not any(owner == session for owner, _ in runtime.calls)
        assert not any(entry.session == session for entry in runtime.store.entries.values())
        with pytest.raises(SessionClosed):
            await run.metadata(allow_failed=True)
    finally:
        folder_paths.set_output_directory(original_output)
        await runtime.close()
