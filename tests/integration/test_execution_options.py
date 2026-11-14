from comani import Client


class HiddenAuth:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"extra_data": ("STRING",)},
                "hidden": {"auth": "AUTH_TOKEN_COMFY_ORG", "key": "API_KEY_COMFY_ORG"}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, extra_data, auth=None, key=None):
        return ({"port": extra_data, "auth": auth, "key": key},)


async def test_hidden_auth_metadata_uses_existing_native_protocol_without_api_calls(native_http):
    runtime, url = native_http
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS["ComaniTestHiddenAuth"] = HiddenAuth
    metadata = {"auth_token_comfy_org": "dummy-auth", "api_key_comfy_org": "dummy-key"}
    async with Client(url) as remote:
        eager = remote.node("ComaniTestHiddenAuth", {"extra_data": "actual-port"}, extra_data=metadata)
        assert await (await eager.output()).fetch() == {"port": "actual-port", "auth": "dummy-auth", "key": "dummy-key"}
        ordinary = remote.node("ComaniTestHiddenAuth", extra_data="ordinary-port")
        assert await (await ordinary.output()).fetch() == {"port": "ordinary-port", "auth": None, "key": None}
        with remote.graph() as graph:
            value, = await remote.node("ComaniTestHiddenAuth", extra_data="captured-port")
            graph.output("answer", value)
        run = remote.submit(graph, extra_data=metadata)
        assert await (await run.output("answer")).fetch() == {"port": "captured-port", "auth": "dummy-auth", "key": "dummy-key"}
        raw = {"node": {"class_type": "ComaniTestHiddenAuth", "inputs": {"extra_data": "raw-port"}}}
        run = remote.submit(raw, outputs={"answer": ["node", 0]}, targets=["node"], extra_data=metadata)
        assert await (await run.output("answer")).fetch() == {"port": "raw-port", "auth": "dummy-auth", "key": "dummy-key"}
