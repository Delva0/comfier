"""A single code upload can overlap an earlier admission without reordering calls."""

import asyncio

import pytest

from comfier import Client, SessionClosed

from .test_submit_batch import BatchRuntime, TraceTransport


class AdmissionUploadGate(TraceTransport):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.accepted = asyncio.Event()
        self.finish_ack = asyncio.Event()
        self.upload_started = asyncio.Event()
        self.finish_upload = asyncio.Event()
        self.upload_finished = asyncio.Event()
        self.upload_cancelled = asyncio.Event()
        self.uploads = 0

    async def request(self, method, **params):
        if method == "artifact_put":
            self.uploads += 1
            self.upload_started.set()
            try:
                await self.finish_upload.wait()
            except asyncio.CancelledError:
                self.upload_cancelled.set()
                raise
            result = await super().request(method, **params)
            self.upload_finished.set()
            return result
        result = await super().request(method, **params)
        if method == "submit" and not self.accepted.is_set():
            self.accepted.set()
            await self.finish_ack.wait()
        return result


async def test_only_next_upload_overlaps_ack_while_submissions_stay_in_order():
    runtime = BatchRuntime()
    transport = AdmissionUploadGate(runtime)
    remote = Client(transport=transport)
    try:
        first = remote.node("Echo", value=41, return_call=True)
        second = remote.python(lambda value: value + 1, inputs={"value": first.output(0)},
                               return_call=True)
        third = remote.python(lambda value: value + 2, inputs={"value": second.output(0)},
                              return_call=True)
        await asyncio.wait_for(transport.accepted.wait(), 1)
        await asyncio.wait_for(transport.upload_started.wait(), 1)
        assert transport.uploads == 1
        assert [call_id for call_id, _ in runtime.submissions] == [first.id]
        transport.finish_upload.set()
        await asyncio.wait_for(transport.upload_finished.wait(), 1)
        assert transport.uploads == 1
        assert [call_id for call_id, _ in runtime.submissions] == [first.id]
        transport.finish_ack.set()
        assert await (await third.output(0)).fetch() == 44
        assert [call_id for call_id, _ in runtime.submissions] == [first.id, second.id, third.id]
    finally:
        transport.finish_ack.set()
        transport.finish_upload.set()
        await remote.close(wait=False)


async def test_close_cancels_lookahead_upload_and_does_not_admit_the_held_call():
    runtime = BatchRuntime()
    transport = AdmissionUploadGate(runtime)
    remote = Client(transport=transport)
    try:
        first = remote.node("Echo", value=41, return_call=True)
        second = remote.python(lambda value: value + 1, inputs={"value": first.output(0)},
                               return_call=True)
        await asyncio.wait_for(transport.accepted.wait(), 1)
        await asyncio.wait_for(transport.upload_started.wait(), 1)
        await remote.close(wait=False)
        await asyncio.wait_for(transport.upload_cancelled.wait(), 1)
        await asyncio.wait_for(remote._queue.join(), 1)
        assert [call_id for call_id, _ in runtime.submissions] == [first.id]
        for call in (first, second):
            with pytest.raises(SessionClosed):
                await call
    finally:
        transport.finish_ack.set()
        transport.finish_upload.set()
        await remote.close(wait=False)
