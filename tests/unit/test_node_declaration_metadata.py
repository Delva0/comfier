"""Declaration preparation preserves management-only connections and epochs."""

import pytest

from comfier import Client, InvalidReference, UnknownOutputLayout

from .test_management_sdk import managed_server


def assert_management_only(remote, supervisor):
    assert supervisor.process is None
    assert remote.epoch == "" and remote._compute_opening is None
    assert remote._lease is None and remote._heartbeat_task is None
    assert remote._recovery is None and remote._cleanup.session_id is None


async def test_uninstalled_worker_still_supports_management_without_compute_attachment(tmp_path):
    async with managed_server(tmp_path) as (_, supervisor, url):
        async with Client(url) as remote:
            assert remote.management_handshake.get("node_metadata") is None
            assert not (await remote.comfyui.info()).installed
            assert (await remote.bash.run("printf declaration-metadata-only")).stdout == "declaration-metadata-only"
            assert_management_only(remote, supervisor)
            with pytest.raises(UnknownOutputLayout):
                remote.node("AbsentNative")
            assert_management_only(remote, supervisor)


async def test_live_declaration_snapshot_refreshes_epoch_without_compute_or_stale_graph_mix(tmp_path):
    async with managed_server(tmp_path) as (_, supervisor, url):
        supervisor.state = "running"
        supervisor.runtime_epoch = "observed-worker-one"
        supervisor._node_metadata = {"epoch": supervisor.runtime_epoch, "nodes": {
            "Pair": {"outputs": ["*", "*"], "output_layout": "fixed", "output_count": 2}}}
        async with Client(url) as remote:
            assert remote._node_metadata_epoch == "observed-worker-one"
            assert_management_only(remote, supervisor)
            with remote.graph():
                first, second = remote.node("Pair")
                assert (first.slot, second.slot) == (0, 1)
                supervisor.runtime_epoch = "observed-worker-two"
                supervisor._node_metadata = {"epoch": supervisor.runtime_epoch, "nodes": {
                    "Pair": {"outputs": ["*"] * 3, "output_layout": "fixed", "output_count": 3}}}
                await remote.comfyui.info()
                with pytest.raises(InvalidReference, match="epoch"):
                    remote.node("Pair")
            with remote.graph():
                first, second, third = remote.node("Pair")
                assert third.slot == 2
            assert remote._node_metadata_epoch == "observed-worker-two"
            assert_management_only(remote, supervisor)
            supervisor.state = "stopped"
            await remote.comfyui.info()
            with pytest.raises(UnknownOutputLayout):
                remote.node("Pair")
            assert remote._node_metadata_epoch is None
            assert_management_only(remote, supervisor)
