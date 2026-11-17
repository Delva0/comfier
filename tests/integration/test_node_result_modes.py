"""Native CPU acceptance of pending whole-result dependencies and their leases."""

import asyncio
import gc
import threading
import time

import pytest

from comfier import Client, Expired, InProcessTransport, InvalidReference, RemoteError

pytestmark = pytest.mark.native


async def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def owned_count(runtime, session):
    with runtime.store.lock:
        return sum(entry.owner == "owned" and entry.session == session
                   for entry in runtime.store.entries.values())


class WholeProducer:
    RETURN_TYPES = ("*", "*", "*")
    FUNCTION = "run"
    OUTPUT_NODE = True
    started = threading.Event()
    gate = threading.Event()
    runs = 0

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"count": ("INT",), "value": ("INT",),
                             "blocked": ("BOOLEAN",), "fail": ("BOOLEAN",)}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, count, value, blocked, fail):
        type(self).runs += 1
        type(self).started.set()
        if blocked and not type(self).gate.wait(10):
            raise RuntimeError("acceptance gate timed out")
        if fail:
            raise ValueError("native whole producer failed")
        return tuple(value + index for index in range(count))


class WholeConsumer:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",), "fail": ("BOOLEAN",)}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, value, fail):
        if fail:
            raise ValueError("native whole consumer failed")
        return (value,)


class WholeStreamProducer:
    RETURN_TYPES = ("*", "*")
    OUTPUT_IS_LIST = (True, False)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"length": ("INT",), "outputs": ("INT",)}}

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, length, outputs):
        WholeProducer.started.set()
        if not WholeProducer.gate.wait(10):
            raise RuntimeError("stream acceptance gate timed out")
        return (list(range(length)), 42)[:outputs]


class WholeNoneProducer:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def run(self):
        return (None,)


class WholeTypedConsumer(WholeConsumer):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT",), "fail": ("BOOLEAN",)}}


@pytest.fixture
def whole_nodes(native_http):
    runtime, url = native_http
    WholeProducer.started = threading.Event()
    WholeProducer.gate = threading.Event()
    WholeProducer.runs = 0
    mappings = runtime.adapter.nodes.NODE_CLASS_MAPPINGS
    mappings["AcceptanceWholeProducer"] = WholeProducer
    mappings["AcceptanceWholeConsumer"] = WholeConsumer
    mappings["AcceptanceWholeStream"] = WholeStreamProducer
    mappings["AcceptanceWholeNone"] = WholeNoneProducer
    mappings["AcceptanceWholeTypedConsumer"] = WholeTypedConsumer
    try:
        yield runtime, url
    finally:
        WholeProducer.gate.set()
        mappings.pop("AcceptanceWholeProducer", None)
        mappings.pop("AcceptanceWholeConsumer", None)
        mappings.pop("AcceptanceWholeStream", None)
        mappings.pop("AcceptanceWholeNone", None)
        mappings.pop("AcceptanceWholeTypedConsumer", None)


def producer(remote, count, *, blocked=True, fail=False):
    return remote.node("AcceptanceWholeProducer", declared_outputs=False,
                       count=count, value=40, blocked=blocked, fail=fail)


def producer_state(runtime, session):
    return next(state for (owner, _), state in runtime.calls.items()
                if owner == session and state.spec.get("node_type") == "AcceptanceWholeProducer")


@pytest.mark.parametrize("actual", [0, 1, 3])
async def test_pending_whole_results_normalize_nested_inputs_and_release_leases(whole_nodes, actual):
    runtime, url = whole_nodes
    async with Client(url) as remote:
        whole = producer(remote, actual)
        await eventually(WholeProducer.started.is_set)
        source = producer_state(runtime, remote.session_id)
        source_key = (remote.session_id, source.call_id)
        assert not source.done.done()
        call = remote.node("AcceptanceWholeConsumer", return_call=True,
                           value={"list": [whole, whole], "tuple": (whole,)}, fail=False)
        consumer_id = call.id
        await eventually(lambda: (remote.session_id, consumer_id) in runtime.calls)
        assert not source.done.done() and sum(source.reservations.values()) > 0
        del whole
        gc.collect()
        assert source_key in runtime.calls
        WholeProducer.gate.set()
        result, = await call
        expected = None if actual == 0 else 40 if actual == 1 else (40, 41, 42)
        assert await result.fetch() == {"list": [expected, expected], "tuple": (expected,)}
        await runtime.queue.join()
        await eventually(lambda: source_key not in runtime.calls)
        assert all(value == 0 for value in source.reservations.values())
        assert owned_count(runtime, remote.session_id) == 1
        assert WholeProducer.runs == 1
        del call, result
        gc.collect()
        await eventually(lambda: owned_count(runtime, remote.session_id) == 0)


@pytest.mark.parametrize("fail_at", ["producer", "consumer"])
async def test_whole_dependency_failure_settles_and_releases_all_leases(whole_nodes, fail_at):
    runtime, url = whole_nodes
    async with Client(url) as remote:
        whole = producer(remote, 3, fail=fail_at == "producer")
        await eventually(WholeProducer.started.is_set)
        source = producer_state(runtime, remote.session_id)
        source_key = (remote.session_id, source.call_id)
        call = remote.node("AcceptanceWholeConsumer", return_call=True,
                           value=whole, fail=fail_at == "consumer")
        await eventually(lambda: (remote.session_id, call.id) in runtime.calls)
        del whole
        gc.collect()
        WholeProducer.gate.set()
        with pytest.raises(RemoteError, match=f"native whole {fail_at} failed"):
            await asyncio.wait_for(call, 5)
        await runtime.queue.join()
        gc.collect()
        await eventually(lambda: source_key not in runtime.calls)
        assert all(value == 0 for value in source.reservations.values())
        await eventually(lambda: owned_count(runtime, remote.session_id) == 0)


class LostAcceptanceTransport(InProcessTransport):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.drop_next_submit = False
        self.submits = []
        self.resumes = 0

    async def request(self, method, **params):
        if method == "submit":
            self.submits.append((params["call_id"], params["spec"]))
        if method == "resume":
            self.resumes += 1
        value = await super().request(method, **params)
        if method == "submit" and self.drop_next_submit:
            self.drop_next_submit = False
            raise ConnectionError("lost native whole-consumer acceptance reply")
        return value


async def test_whole_consumer_recovery_does_not_duplicate_reservations_or_execution(whole_nodes):
    runtime, _ = whole_nodes
    transport = LostAcceptanceTransport(runtime)
    async with Client(transport=transport) as remote:
        whole = producer(remote, 3)
        await eventually(WholeProducer.started.is_set)
        source = producer_state(runtime, remote.session_id)
        transport.drop_next_submit = True
        call = remote.node("AcceptanceWholeConsumer", return_call=True, value=whole, fail=False)
        await eventually(lambda: transport.resumes == 1 and len(transport.submits) == 3)
        assert transport.submits[1] == transport.submits[2]
        assert sum(source.reservations.values()) == 1
        del whole
        gc.collect()
        WholeProducer.gate.set()
        value, = await call
        assert await value.fetch() == (40, 41, 42)
        assert WholeProducer.runs == 1
        await runtime.queue.join()
        assert all(count == 0 for count in source.reservations.values())


async def test_whole_proxy_rejects_cross_session_and_expired_epoch_before_consumer_submit(whole_nodes):
    runtime, url = whole_nodes
    async with Client(url) as first, Client(url) as second:
        whole = producer(first, 2)
        await eventually(WholeProducer.started.is_set)
        before = len(runtime.calls)
        with pytest.raises(InvalidReference):
            second.node("AcceptanceWholeConsumer", return_call=True, value=whole, fail=False)
        assert len(runtime.calls) == before
        first._expire_compute(Expired("acceptance runtime epoch changed"))
        with pytest.raises(Expired, match="epoch changed"):
            first.node("AcceptanceWholeConsumer", return_call=True, value=whole, fail=False)
        assert len(runtime.calls) == before
        WholeProducer.gate.set()


async def test_native_graph_explicit_slots_execute_and_whole_graph_edges_are_rejected(whole_nodes):
    runtime, url = whole_nodes
    async with Client(url) as remote:
        with remote.graph() as graph:
            whole = producer(remote, 2, blocked=False)
            with pytest.raises(TypeError, match="[Gg]raph|unpack|slot"):
                remote.node("AcceptanceWholeConsumer", value=whole, fail=False)
            first, second = await whole
            result = remote.node("AcceptanceWholeConsumer", value=second, fail=False)
            assert await result is result
            graph.output("value", result)
        assert not runtime.calls and WholeProducer.runs == 0
        run = remote.submit(graph, return_call=True)
        assert await (await run.output("value")).fetch() == 41
        assert await (await run[first]).fetch() == 40
        downstream = remote.node("AcceptanceWholeConsumer", value=run[second], fail=False)
        assert await (await downstream).fetch() == 41


@pytest.mark.parametrize("value", [{"items": [1, 2], "pair": (3, 4)}, (1, 2, 3)])
async def test_python_container_is_one_output_value_when_passed_to_native_node(whole_nodes, value):
    _, url = whole_nodes
    async with Client(url) as remote:
        python_call = remote.python(lambda item: item, inputs={"item": value}, return_call=True)
        reference, = await python_call
        assert await reference.fetch() == value
        native = remote.node("AcceptanceWholeConsumer", value=python_call.output(), fail=False)
        assert await (await native).fetch() == value


@pytest.mark.parametrize("actual", [1, 2])
@pytest.mark.parametrize("length", [0, 1, 2])
@pytest.mark.parametrize("nested", [False, True])
async def test_whole_stream_matches_awaited_reference_input(whole_nodes, actual, length, nested):
    """The public await-and-forward spelling is the native value/stream oracle."""
    _, url = whole_nodes

    async def outcome(call):
        try:
            reference, = await call
            return "ready", await reference.fetch()
        except RemoteError as error:
            return "error", type(error).__name__, str(error).splitlines()[0]

    async with Client(url) as remote:
        whole = remote.node("AcceptanceWholeStream", declared_outputs=False,
                            outputs=actual, length=length)
        await eventually(WholeProducer.started.is_set)
        direct = remote.node("AcceptanceWholeConsumer", return_call=True, fail=False,
                             value={"whole": whole} if nested else whole)
        WholeProducer.gate.set()
        ready = await whole
        forwarded = remote.node("AcceptanceWholeConsumer", return_call=True, fail=False,
                                value={"whole": ready} if nested else ready)
        assert await outcome(direct) == await outcome(forwarded)


@pytest.mark.parametrize("one_none_slot", [False, True])
async def test_zero_whole_is_literal_none_while_a_real_none_slot_remains_a_reference(
        whole_nodes, one_none_slot):
    _, url = whole_nodes

    async def outcome(call):
        try:
            reference, = await call
            return "ready", await reference.fetch()
        except RemoteError as error:
            return "error", type(error).__name__, str(error).splitlines()[0]

    async with Client(url) as remote:
        if one_none_slot:
            whole = remote.node("AcceptanceWholeNone", declared_outputs=False)
        else:
            whole = producer(remote, 0, blocked=False)
        direct = remote.node("AcceptanceWholeTypedConsumer", return_call=True, fail=False,
                             value=whole)
        ready = await whole
        assert (ready is not None) is one_none_slot
        forwarded = remote.node("AcceptanceWholeTypedConsumer", return_call=True, fail=False,
                                value=ready)
        direct_result, forwarded_result = await asyncio.gather(outcome(direct), outcome(forwarded))
        assert direct_result == forwarded_result
        assert direct_result[0] == ("ready" if one_none_slot else "error")
