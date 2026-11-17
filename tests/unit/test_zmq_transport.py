"""Real-socket checks for optional compute transport boundary guarantees."""

import asyncio
import time

import pytest

from comani import ZMQTransport
from comani.daemon.supervisor import RuntimeConfig, RuntimeSupervisor
from comani.daemon.zmq import ZMQServer
from comani.errors import RemoteError, SessionClosed

zmq = pytest.importorskip("zmq")


class Runtime:
    async def handshake(self, **params):
        return {"session_id": "issued", "epoch": "same-runtime", "capabilities": {
            "files": 1, "files_resume": 1, "binary_fetch": 1,
            "binary_fetch_lz4": 1, "session_resume": 1}}

    async def heartbeat(self, **params):
        return {"session_id": params["session_id"]}

    async def wait_result(self, **params):
        await asyncio.sleep(params["wait_seconds"])
        return {"pending": True}


@pytest.mark.parametrize("kind", ["ipc", "tcp"])
async def test_real_endpoint_auth_reserved_session_and_compute_capabilities(tmp_path, kind):
    address = "ipc://" + str(tmp_path / "compute.sock") if kind == "ipc" else "tcp://127.0.0.1:*"
    async with ZMQServer(Runtime(), endpoints=[address], token="test-secret") as server:
        transport = ZMQTransport(server.endpoints[0], token="test-secret", timeout=1)
        try:
            hello = await transport.request("handshake")
            assert hello["capabilities"] == {"binary_fetch": 1, "session_resume": 1, "compute_zmq": 1}
            for method in ("handshake", "heartbeat", "close_session"):
                with pytest.raises(SessionClosed, match="reserved"):
                    await transport.request(method, session_id="raw")
        finally:
            await transport.close()
        denied = ZMQTransport(server.endpoints[0], token="wrong", timeout=1)
        try:
            with pytest.raises(RemoteError) as failure:
                await denied.request("handshake")
            assert failure.value.details["code"] == "Unauthorized"
        finally:
            await denied.close()


async def test_no_peer_deadline_includes_socket_send(tmp_path):
    transport = ZMQTransport("ipc://" + str(tmp_path / "absent.sock"), token="secret", timeout=.08)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            await transport.request("handshake")
        assert time.monotonic() - started < 1
        assert not transport._pending
    finally:
        await transport.close()


async def test_wait_credit_reserves_capacity_for_control_requests():
    async with ZMQServer(Runtime(), endpoints=["tcp://127.0.0.1:*"], token="secret") as server:
        transport = ZMQTransport(server.endpoints[0], token="secret", timeout=1, max_inflight=4)
        waiting = [asyncio.create_task(transport.request("wait_result", wait_seconds=.4)) for _ in range(5)]
        try:
            await asyncio.sleep(.05)
            async with asyncio.timeout(.2):
                assert await transport.request("heartbeat", session_id="issued") == {"session_id": "issued"}
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
            await transport.close()


@pytest.mark.parametrize("as_text", [False, True])
async def test_curve_pins_server_and_wrong_key_has_a_bounded_deadline(as_text):
    public, secret = zmq.curve_keypair()
    if as_text:
        public, secret = public.decode(), secret.decode()
    async with ZMQServer(Runtime(), endpoints=["tcp://127.0.0.1:*"], token="secret",
                         curve_public_key=public, curve_secret_key=secret) as server:
        good = ZMQTransport(server.endpoints[0], token="secret", timeout=1, curve_server_key=public)
        bad = ZMQTransport(server.endpoints[0], token="secret", timeout=.15,
                           curve_server_key=zmq.curve_keypair()[0])
        try:
            assert (await good.request("handshake"))["epoch"] == "same-runtime"
            with pytest.raises(TimeoutError):
                await bad.request("handshake")
        finally:
            await good.close()
            await bad.close()


def test_remote_tcp_requires_encryption():
    with pytest.raises(ValueError, match="CURVE"):
        ZMQTransport("tcp://192.0.2.1:9000", token="secret")
    with pytest.raises(ValueError, match="CURVE"):
        ZMQServer(Runtime(), endpoints=["tcp://*:9000"], token="secret")


@pytest.mark.parametrize("address", ["tcp://127.0.0.1:*", "tcp://*:9000", "tcp://:9000", "tcp://::1:9000"])
def test_connect_requires_a_concrete_valid_endpoint(address):
    with pytest.raises(ValueError):
        ZMQTransport(address, token="secret", curve_server_key=zmq.curve_keypair()[0])


def test_curve_keys_are_validated_without_including_the_supplied_value():
    bad = "do-not-echo-private-key"
    with pytest.raises(ValueError) as error:
        ZMQServer(Runtime(), endpoints=["tcp://127.0.0.1:9000"], token="secret",
                  curve_public_key=bad, curve_secret_key=bad)
    assert bad not in str(error.value)


async def test_bound_peer_without_reply_has_a_deadline_and_can_reconnect():
    context = zmq.asyncio.Context()
    socket = context.socket(zmq.ROUTER)
    port = socket.bind_to_random_port("tcp://127.0.0.1")
    transport = ZMQTransport(f"tcp://127.0.0.1:{port}", token="secret", timeout=.08)
    try:
        with pytest.raises(TimeoutError):
            await transport.request("handshake")
        assert not transport._pending
        socket.close(0)
        async with ZMQServer(Runtime(), endpoints=["tcp://127.0.0.1:*"], token="secret") as server:
            transport.timeout = 1
            await transport.reconnect(server.endpoints[0])
            assert (await transport.request("handshake"))["epoch"] == "same-runtime"
    finally:
        await transport.close()
        socket.close(0)
        context.destroy(linger=0)


async def test_supervisor_lifecycle_rpc_uses_the_selected_compute_channel(tmp_path):
    from types import SimpleNamespace

    supervisor = RuntimeSupervisor(RuntimeConfig(str(tmp_path), compute_transport="zmq"))
    seen = []

    async def request(method, **params):
        seen.append((method, params))
        return {"epoch": "ipc-runtime"}

    async def no_http():
        raise AssertionError("ZMQ configuration must not probe or drain through HTTP")

    supervisor.process = SimpleNamespace(returncode=None)
    supervisor._compute_transport = SimpleNamespace(request=request)
    supervisor._http = no_http
    assert await supervisor.runtime_rpc("runtime_info", {"include_node_metadata": True}) == {"epoch": "ipc-runtime"}
    await supervisor.runtime_rpc("file_invalidate", {"category": "checkpoints"})
    assert seen == [("runtime_info", {"include_node_metadata": True}),
                    ("file_invalidate", {"category": "checkpoints"})]


def test_runtime_config_compute_transport_is_explicit(tmp_path):
    assert RuntimeConfig(str(tmp_path)).compute_transport == "http"
    with pytest.raises(ValueError, match="compute_transport"):
        RuntimeConfig(str(tmp_path), compute_transport="auto")


@pytest.mark.parametrize("replace", [False, True])
async def test_server_removes_only_its_own_ipc_socket_on_close(tmp_path, replace):
    path = tmp_path / "managed.sock"
    async with ZMQServer(Runtime(), endpoints=["ipc://" + str(path)], token="secret"):
        assert path.is_socket()
        if replace:
            path.unlink()
            path.write_text("external replacement")
    if replace:
        assert path.read_text() == "external replacement"
    else:
        assert not path.exists()
