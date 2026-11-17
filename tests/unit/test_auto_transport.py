"""Default discovery and route recovery over real HTTP and compute sockets."""

from contextlib import asynccontextmanager

import cloudpickle
import pytest
from aiohttp import web

import comfier as cf
from comfier.daemon.management_http import create_management_app
from comfier.daemon.supervisor import RuntimeConfig, RuntimeSupervisor
from comfier.daemon.zmq import ZMQServer
from comfier.protocol import PICKLE_CONTENT_TYPE

from .test_binary_fetch import serve
from .test_sdk_recovery import RecoverableRuntime

zmq = pytest.importorskip("zmq")


class Runtime(RecoverableRuntime):
    async def handshake(self, session_id):
        result = await super().handshake(session_id)
        result["capabilities"]["binary_fetch"] = 1
        return result

    async def runtime_info(self, **params):
        return {"epoch": self.epoch}

    async def heartbeat(self, **params):
        return {}

    async def wait_result(self, session_id, call_id, node_id=None, wait_seconds=None):
        return await super().wait_result(session_id, call_id, node_id)

    async def fetch_bytes(self, session_id, ref_id, epoch):
        return cloudpickle.dumps(self.objects[ref_id])


@asynccontextmanager
async def endpoint(tmp_path, *, token=None, auto_compute=True, internal_compute=False, **gateway_options):
    runtime = Runtime()
    methods = []

    async def dispatch(method, **params):
        function = getattr(runtime, "fetch_bytes" if method == "fetch" else method)
        return await function(**params)

    async def rpc(request):
        body = await request.json()
        method, params = body["method"], body["params"]
        methods.append((method, params))
        try:
            if method == "fetch" and request.headers.get("Accept") == PICKLE_CONTENT_TYPE:
                return web.Response(body=await runtime.fetch_bytes(**params), content_type=PICKLE_CONTENT_TYPE)
            return web.json_response({"result": await getattr(runtime, method)(**params)})
        except Exception as error:
            from comfier.errors import error_to_wire
            return web.json_response({"error": error_to_wire(error)}, status=400)

    app = web.Application()
    app.router.add_post("/rpc", rpc)
    async with serve(app) as worker_url:
        supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path), workspace=str(tmp_path)))
        supervisor.endpoint, supervisor.state = worker_url, "running"
        supervisor.runtime_epoch = runtime.epoch
        supervisor._node_metadata = {"epoch": runtime.epoch, "nodes": {
            "Echo": {"outputs": ["*"]}, "Effect": {"outputs": []}}}
        supervisor.runtime_request = dispatch
        if internal_compute:
            from types import SimpleNamespace

            async def close():
                pass

            supervisor._compute_transport = SimpleNamespace(close=close)
        async with serve(create_management_app(supervisor, token=token, auto_compute=auto_compute,
                                                **gateway_options)) as url:
            yield url, runtime, methods


@pytest.mark.parametrize("token", [None, "configured-token"])
async def test_default_client_uses_verified_private_ipc_without_protocol_options(tmp_path, token):
    async with endpoint(tmp_path, token=token) as (url, runtime, methods):
        async with cf.Client(url, token=token) as remote:
            assert runtime.handshakes == 0
            value = await remote.node("Echo", value={"answer": 42})
            assert await value.fetch() == {"answer": 42}
            assert remote.transport.selected_transport == "zmq"
            assert remote.transport.selection_reason == "verified_local_ipc"
            assert runtime.handshakes == 1
            assert [method for method, _ in methods if method != "heartbeat"] == ["handshake"]


async def test_http_only_daemon_keeps_default_client_and_one_session(tmp_path):
    async with endpoint(tmp_path, auto_compute=False) as (url, runtime, methods):
        async with cf.Client(url) as remote:
            assert await (await remote.node("Echo", value="legacy")).fetch() == "legacy"
            assert remote.transport.selected_transport == "http"
            assert remote.transport.selection_reason == "http_only_daemon"
            assert runtime.handshakes == 1
            assert "fetch" in [method for method, _ in methods]


@pytest.mark.parametrize("kind", ["identity", "epoch", "namespace", "dependency"])
async def test_unverified_candidates_fall_back_without_opening_extra_sessions(tmp_path, monkeypatch, kind):
    async with endpoint(tmp_path) as (url, runtime, _):
        async with cf.Client(url) as remote:
            candidate = remote.transport._discovery["compute_endpoints"][0]
            if kind == "identity":
                candidate["server_id"] = "another-daemon"
            elif kind == "epoch":
                original = cf.ZMQTransport.request

                async def wrong_epoch(transport, method, **params):
                    result = await original(transport, method, **params)
                    return {**result, "epoch": "another-runtime"} if method == "runtime_info" else result

                monkeypatch.setattr(cf.ZMQTransport, "request", wrong_epoch)
            elif kind == "namespace":
                candidate["endpoint"] = "ipc://" + str(tmp_path / "missing.sock")
            else:
                def missing_dependency(*args, **kwargs):
                    raise ImportError("unavailable in this interpreter")
                monkeypatch.setattr("comfier._auto_transport.ZMQTransport", missing_dependency)
            assert await (await remote.node("Echo", value=42)).fetch() == 42
            assert remote.transport.selected_transport == "http"
            assert runtime.handshakes == 1


async def test_lost_compute_ack_resumes_http_with_same_admission_identity(tmp_path):
    async with endpoint(tmp_path) as (url, runtime, methods):
        async with cf.Client(url) as remote:
            await remote._attach()
            compute = remote.transport._compute
            original, dropped = compute.request, False

            async def lose_ack(method, **params):
                nonlocal dropped
                result = await original(method, **params)
                if method == "submit" and not dropped:
                    dropped = True
                    raise ConnectionError("admission response lost")
                return result

            compute.request = lose_ack
            assert await remote.node("Effect", return_call=True, value="once") == ()
            assert runtime.effects == [{"value": "once"}]
            assert runtime.handshakes == runtime.resumes == 1
            assert runtime.submit_attempts == 2 and runtime.windows[0] == runtime.windows[1]
            assert remote.transport.selection_reason == "compute_connection_lost"
            assert [method for method, _ in methods if method != "heartbeat"][:3] == ["handshake", "resume", "submit"]


async def test_expired_original_session_never_opens_another_session_on_downgrade(tmp_path):
    async with endpoint(tmp_path) as (url, runtime, _):
        async with cf.Client(url) as remote:
            await remote._attach()

            async def disconnected(*args, **kwargs):
                raise ConnectionError("offline compute")

            async def closed_session(**params):
                raise cf.SessionClosed("original session revoked")

            remote.transport._compute.request = disconnected
            runtime.resume = closed_session
            with pytest.raises(cf.SessionClosed, match="revoked"):
                await remote.node("Effect", return_call=True, value="never")
            assert runtime.handshakes == 1 and runtime.effects == []


async def test_curve_advertisement_is_pinned_and_selected_without_client_options(tmp_path):
    async with endpoint(tmp_path, token="secret", auto_compute=False) as (url, runtime, _):
        public, secret = zmq.curve_keypair()
        async with ZMQServer(runtime, endpoints=["tcp://127.0.0.1:*"], token="secret",
                             curve_public_key=public, curve_secret_key=secret) as server:
            async with cf.Client(url, token="secret") as remote:
                remote.transport._discovery.update(capabilities={"compute_zmq": 1}, compute_endpoints=[{
                    "endpoint": server.endpoints[0], "protocol": "zmq-v1", "server_id": server.server_id,
                    "curve_server_key": public.decode()}])
                assert await (await remote.node("Echo", value="encrypted")).fetch() == "encrypted"
                assert remote.transport.selection_reason == "verified_curve_tcp"
                assert runtime.handshakes == 1


async def test_wildcard_tcp_discovery_uses_daemon_host_and_actual_compute_port(tmp_path):
    public, secret = zmq.curve_keypair()
    async with endpoint(tmp_path, token="secret", compute_endpoints=["tcp://0.0.0.0:*"],
                        curve_public_key=public, curve_secret_key=secret) as (url, _, _):
        async with cf.Client(url, token="secret") as remote:
            assert await (await remote.node("Echo", value=42)).fetch() == 42
            assert remote.transport.selection_reason == "verified_curve_tcp"
            assert remote.transport.compute_endpoint.startswith("tcp://127.0.0.1:")
            assert remote.transport.compute_endpoint.rsplit(":", 1)[1] != url.rsplit(":", 1)[1]


async def test_http_lz4_remains_available_behind_private_compute_bridge(tmp_path, monkeypatch):
    pytest.importorskip("lz4")
    from comfier.daemon import management_http

    compressed = []
    original = management_http.compress_pickle

    def observe(payload):
        result = original(payload)
        compressed.append(result is not None)
        return result

    monkeypatch.setattr(management_http, "compress_pickle", observe)
    async with endpoint(tmp_path, internal_compute=True) as (url, _, _):
        async with cf.Client(transport=cf.HTTPTransport(url)) as remote:
            payload = b"a" * (2 * 1024**2)
            assert await (await remote.node("Echo", value=payload)).fetch() == payload
            assert remote.transport._binary_fetch_lz4
            assert compressed == [True]


async def test_runtime_reset_retires_compute_socket_before_new_epoch_attachment(tmp_path):
    async with endpoint(tmp_path) as (url, _, _):
        async with cf.Client(url) as remote:
            await remote._attach()
            original = remote.transport._compute
            await remote._reset_compute()
            assert original._closed and remote.transport._compute is None
            await remote._attach()
            assert remote.transport._compute is not original
            assert remote.transport.selected_transport == "zmq"


async def test_large_fetch_uses_http_stream_without_socket_frame_limit(tmp_path):
    async with endpoint(tmp_path, token="secret", auto_compute=False) as (url, runtime, methods):
        async with ZMQServer(runtime, endpoints=["ipc://" + str(tmp_path / "limited.sock")],
                             token="secret", max_response_bytes=512) as server:
            async with cf.Client(url, token="secret") as remote:
                remote.transport._discovery.update(capabilities={"compute_zmq": 1}, compute_endpoints=[{
                    "endpoint": server.endpoints[0], "protocol": "zmq-v1", "server_id": server.server_id}])
                payload = b"a" * 4096
                reference = await remote.node("Echo", value=payload)
                assert await reference.fetch() == payload
                assert remote.transport.selected_transport == "zmq"
                assert runtime.handshakes == 1 and runtime.resumes == 0
                assert len([method for method, _ in methods if method == "fetch"]) == 1


async def test_failed_automatic_gateway_bind_keeps_management_and_http_compute(tmp_path, monkeypatch):
    async def cannot_bind(server):
        raise OSError("IPC is unavailable in this namespace")

    monkeypatch.setattr(ZMQServer, "start", cannot_bind)
    async with endpoint(tmp_path) as (url, runtime, _):
        async with cf.Client(url) as remote:
            assert await (await remote.node("Echo", value=42)).fetch() == 42
            assert remote.transport.selected_transport == "http" and runtime.handshakes == 1


async def test_supervisor_auto_downgrade_raises_uncertainty_before_replaying(tmp_path):
    from types import SimpleNamespace

    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path)))
    calls = []

    async def lost(method, **params):
        calls.append((method, params))
        raise ConnectionError("ack lost")

    async def close():
        pass

    supervisor._compute_transport = SimpleNamespace(request=lost, close=close)
    supervisor.process = SimpleNamespace(returncode=None)
    with pytest.raises(ConnectionError, match="ack lost"):
        await supervisor.runtime_request("submit", session_id="same", call_id="same")
    assert len(calls) == 1 and supervisor._compute_transport is None
    assert supervisor._compute_fallback == "compute_connection_lost"


async def test_concurrent_downgrade_does_not_close_other_inflight_requests(tmp_path):
    from types import SimpleNamespace

    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path)))
    closed = []

    async def lost(method, **params):
        raise ConnectionError("ack lost")

    async def close():
        closed.append(True)

    transport = SimpleNamespace(request=lost, close=close)
    supervisor._compute_transport = transport
    supervisor.process = SimpleNamespace(returncode=None)
    with pytest.raises(ConnectionError):
        await supervisor.runtime_request("submit", session_id="same", call_id="same")
    assert not closed
    await supervisor._close_compute()
    assert closed == [True]
