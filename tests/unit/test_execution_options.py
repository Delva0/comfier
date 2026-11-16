import pytest


async def test_legacy_node_keywords_remain_real_inputs(peer):
    remote, runtime = peer
    call = remote.node("Echo", return_call=True, inputs="input-port", extra_data="extra-data-port", node_inputs="positional-name-port")
    await call
    spec = runtime.submissions[-1][1]
    assert spec["inputs"] == {"inputs": "input-port", "extra_data": "extra-data-port", "node_inputs": "positional-name-port"}
    assert "extra_data" not in spec


async def test_explicit_node_options_separate_metadata_and_snapshot_it(peer):
    remote, runtime = peer
    metadata = {"api_key_comfy_org": "dummy-key", "extra_pnginfo": {"label": "before"}}
    call = remote.node("Echo", {"value": 7, "extra_data": "real-node-port"}, return_call=True, extra_data=metadata)
    metadata["extra_pnginfo"]["label"] = "after"
    assert await (await call.output()).fetch() == 7
    spec = runtime.submissions[-1][1]
    assert spec["inputs"]["extra_data"] == "real-node-port"
    assert spec["extra_data"] == {"api_key_comfy_org": "dummy-key", "extra_pnginfo": {"label": "before"}}


async def test_workflow_metadata_belongs_to_submit(peer):
    remote, runtime = peer
    with remote.graph() as graph:
        value, = await remote.node("Echo", return_call=True, value=1)
        graph.output("answer", value)
    run = remote.submit(graph, extra_data={"auth_token_comfy_org": "dummy-auth"}, return_call=True)
    await run
    assert runtime.submissions[-1][1]["extra_data"] == {"auth_token_comfy_org": "dummy-auth"}
    with remote.graph():
        with pytest.raises(ValueError, match="whole native run"):
            remote.node("Echo", {"value": 1}, return_call=True, extra_data={"auth_token_comfy_org": "dummy"})
        with pytest.raises(ValueError, match="whole native run"):
            remote.submit(graph, extra_data={"auth_token_comfy_org": "dummy"})


async def test_explicit_options_reject_typos_and_non_json_metadata(peer):
    remote, _ = peer
    with pytest.raises(TypeError, match="Unknown node execution options"):
        remote.node("Echo", {"value": 1}, return_call=True, typo=2)
    with pytest.raises(TypeError, match="input mapping"):
        remote.node("Echo", 7, return_call=True)
    with pytest.raises(TypeError, match="JSON-compatible mapping"):
        remote.node("Echo", {}, return_call=True, extra_data="not-a-mapping")
    with pytest.raises((TypeError, ValueError)):
        remote.node("Echo", {}, return_call=True, extra_data={"object": object()})
