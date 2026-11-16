"""Recovery preserves one session, one admitted effect and existing ownership."""

import asyncio
import time

import pytest

from comani import Client, Expired, InProcessTransport, SessionClosed

from ._helpers import eventually
from .conftest import FakeRuntime


class RecoverableRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.handshakes = self.resumes = self.submit_attempts = 0
        self.windows = []

    async def handshake(self, session_id):
        self.handshakes += 1
        value = await super().handshake(session_id)
        return {**value, "session_id": session_id, "resume_token": "original-authorization",
                "capabilities": {"session_resume": 1},
                "submission_window": {"token": "original-window", "expires_at": time.time() + 60}}

    async def resume(self, session_id, resume_token, epoch):
        self.resumes += 1
        assert resume_token == "original-authorization"
        if epoch != self.epoch:
            raise Expired("Runtime epoch changed")
        return {"session_id": session_id, "epoch": self.epoch}

    async def submit(self, session_id, call_id, spec, submission_window=None):
        self.submit_attempts += 1
        self.windows.append(submission_window)
        if call_id in self.results:
            assert next(original for key, original in self.submissions if key == call_id) == spec
            return {"accepted": True}
        return await super().submit(session_id, call_id, spec)


class DroppingTransport(InProcessTransport):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.drop_once = set()
        self.offline = False
        self.change_epoch = False

    async def request(self, method, **params):
        if self.offline:
            raise ConnectionError("offline")
        value = await super().request(method, **params)
        if method in self.drop_once:
            self.drop_once.remove(method)
            if self.change_epoch:
                self.runtime.epoch = "a-new-runtime"
            raise ConnectionError("response disappeared after acceptance")
        return value


async def test_lost_acceptance_reply_reuses_call_id_spec_and_submission_window():
    runtime = RecoverableRuntime()
    transport = DroppingTransport(runtime)
    transport.drop_once.add("submit")
    async with Client(transport=transport) as remote:
        call = remote.node("Effect", return_call=True, value="exactly one effect")
        assert await call == ()
        assert runtime.effects == [{"value": "exactly one effect"}]
        assert runtime.handshakes == 1 and runtime.resumes == 1
        assert runtime.submit_attempts == 2
        assert runtime.windows[0] == runtime.windows[1]
        assert len(runtime.submissions) == 1


async def test_wait_fetch_and_release_recover_without_replacing_owned_references():
    runtime = RecoverableRuntime()
    transport = DroppingTransport(runtime)
    async with Client(transport=transport) as remote:
        transport.drop_once.add("wait_result")
        call = remote.node("Echo", return_call=True, value="retained")
        a, b = await asyncio.gather(asyncio.ensure_future(call), asyncio.ensure_future(call))
        ref = a[0]
        assert ref is b[0]
        transport.drop_once.add("fetch")
        assert await ref.fetch() == "retained"
        transport.drop_once.add("release")
        key = ref.id
        ref.release()
        await eventually(lambda: runtime.released.count(key) == 2)
        assert runtime.handshakes == 1 and runtime.resumes == 3


async def test_runtime_epoch_change_is_terminal_and_never_creates_a_fresh_session():
    runtime = RecoverableRuntime()
    transport = DroppingTransport(runtime)
    async with Client(transport=transport) as remote:
        ref, = await remote.node("Echo", return_call=True, value=42)
        transport.drop_once.add("fetch")
        transport.change_epoch = True
        with pytest.raises(Expired, match="epoch"):
            await ref.fetch()
        assert runtime.handshakes == 1 and len(runtime.submissions) == 1


async def test_offline_close_revokes_locally_and_reports_unconfirmed_remote_cleanup():
    runtime = RecoverableRuntime()
    transport = DroppingTransport(runtime)
    remote = await Client(transport=transport).start()
    call = remote.node("Block", return_call=True, value=42)
    await eventually(lambda: bool(runtime.submissions))
    transport.offline = True
    ref_call = remote.node("Echo", return_call=True, value=1)
    await eventually(lambda: remote._recovery.reconnecting)
    await asyncio.wait_for(remote.close(), 1)
    assert remote.closed and not remote.close_confirmed
    with pytest.raises(SessionClosed):
        await ref_call
    with pytest.raises(SessionClosed):
        await call
    runtime.gate.set()


async def test_concurrent_sessions_resume_their_own_original_authorization():
    runtimes = [RecoverableRuntime(), RecoverableRuntime()]
    transports = [DroppingTransport(runtime) for runtime in runtimes]
    clients = [await Client(transport=transport).start() for transport in transports]
    try:
        for transport in transports:
            transport.drop_once.add("submit")
        calls = [client.node("Echo", return_call=True, value=index) for index, client in enumerate(clients)]
        result = await asyncio.gather(*(asyncio.ensure_future(call) for call in calls))
        assert await result[0][0].fetch() == 0
        assert await result[1][0].fetch() == 1
        assert clients[0].session_id != clients[1].session_id
        assert all(runtime.handshakes == runtime.resumes == 1 for runtime in runtimes)
    finally:
        await asyncio.gather(*(client.close() for client in clients))
