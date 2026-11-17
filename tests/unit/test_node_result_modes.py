"""Independent user-facing checks for declaration packets and local FX projections."""

import asyncio
import gc
import weakref

import pytest

from comfier import (
    Call,
    Client,
    GraphRef,
    InProcessTransport,
    OutputFuture,
    Released,
    RemoteError,
    UnknownOutputLayout,
)

from ._helpers import eventually
from .conftest import FakeRuntime


class ResultRuntime(FakeRuntime):
    """The wire producer can return a different M from its advertised D."""

    declarations = {"DeclaredTwo": 2, "DeclaredThree": 3, "DeclaredZero": 0}

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.completed = []

    async def handshake(self, session_id):
        payload = await super().handshake(session_id)
        payload["nodes"].update({name: {"outputs": ["*"] * count}
                                 for name, count in self.declarations.items()})
        payload["nodes"]["UnknownArity"] = {}
        return payload

    async def resolve(self, value):
        if isinstance(value, dict) and "$ref" in value and "node_id" in value["$ref"]:
            ref = value["$ref"]
            result = await self.wait_result(None, ref["call_id"], ref["node_id"])
            return self.objects[result["outputs"][ref["slot"]]["id"]]
        return await super().resolve(value)

    async def execute(self, spec, done):
        name = spec.get("node_type")
        if name not in {*self.declarations, "UnknownArity"}:
            return await super().execute(spec, done)
        try:
            inputs = {key: await self.resolve(value) for key, value in spec["inputs"].items()}
            self.started.set()
            if inputs.get("blocked"):
                await self.gate.wait()
            if inputs.get("fail"):
                raise ValueError("controlled producer failure")
            values = inputs.get("values", list(range(self.declarations.get(name, 2))))
            payload = {"outputs": [self.info(value) for value in values],
                       "metadata": {"prompt_id": "native-result",
                                    "executed": ["result"], "ui": {"result": {"ok": True}}}}
            self.completed.append(name)
            done.set_result(payload)
        except Exception as error:
            done.set_exception(error)


@pytest.fixture
async def result_peer():
    runtime = ResultRuntime()
    async with Client(transport=InProcessTransport(runtime)) as client:
        yield client, runtime


async def fetched(outputs):
    return [await (await output).fetch() for output in outputs]


async def test_checked_default_single_pair_and_effect_compose_without_call(peer):
    remote, runtime = peer
    single = remote.node("Echo", value=23)
    assert isinstance(single, OutputFuture) and not isinstance(single, Call)
    pair = remote.node("Pair", value=single)
    assert isinstance(pair, tuple) and len(pair) == 2
    assert all(isinstance(output, OutputFuture) for output in pair)
    empty = remote.node("Effect", value=pair[1])
    assert empty is None
    await eventually(lambda: bool(runtime.effects))
    assert runtime.effects == [{"value": 23}]
    assert await fetched(pair) == [23, 23]


@pytest.mark.parametrize("forward", [False, True])
async def test_checked_unpack_fails_locally_and_does_not_cancel_eager_work(result_peer, forward):
    remote, runtime = result_peer

    def helper():
        return remote.node("DeclaredThree", blocked=True)

    with pytest.raises(ValueError, match="too many values"):
        if forward:
            first, second = helper()
        else:
            first, second = remote.node("DeclaredThree", blocked=True)
    await runtime.started.wait()
    assert not runtime.completed
    runtime.gate.set()
    await eventually(lambda: runtime.completed == ["DeclaredThree"])
    await eventually(lambda: len(runtime.released) == 3)


@pytest.mark.parametrize("return_call", [False, True])
async def test_checked_call_waits_before_python_unpack_and_is_reusable(result_peer, return_call):
    remote, runtime = result_peer
    call = remote.node("DeclaredThree", return_call=return_call, blocked=True)

    async def unpack():
        first, second = await call

    waiter = asyncio.create_task(unpack())
    await runtime.started.wait()
    assert not waiter.done() and not runtime.completed
    runtime.gate.set()
    with pytest.raises(ValueError, match="too many values"):
        await waiter
    assert runtime.completed == ["DeclaredThree"]
    if return_call:
        assert call.status == "completed"
    first, second, third = await call
    assert [await ref.fetch() for ref in (first, second, third)] == [0, 1, 2]
    again = await call
    assert all(left is right for left, right in zip((first, second, third), again))
    assert len(runtime.submissions) == 1


@pytest.mark.parametrize("return_call", [False, True])
async def test_checked_producer_failure_precedes_unpack_error(result_peer, return_call):
    remote, _ = result_peer
    call = remote.node("DeclaredThree", return_call=return_call, fail=True)
    with pytest.raises(RemoteError, match="controlled producer failure"):
        first, second = await call


async def test_checked_gather_keeps_each_declaration_packet(result_peer):
    remote, runtime = result_peer

    def helper(name):
        return remote.node(name, return_call=True)

    calls = [helper("DeclaredTwo"), helper("DeclaredThree")]
    two, three = await asyncio.gather(*calls)
    assert len(two) == 2 and len(three) == 3
    with pytest.raises(ValueError, match="too many values"):
        first, second = three
    assert await two[1].fetch() == 1 and len(runtime.submissions) == 2


@pytest.mark.parametrize("return_call", [False, True])
async def test_checked_uses_declaration_after_actual_outputs_are_known(result_peer, return_call):
    remote, runtime = result_peer
    result = remote.node("DeclaredTwo", return_call=return_call, values=[10, 20, 30])
    if return_call:
        first, second = await result
    else:
        await eventually(lambda: runtime.completed == ["DeclaredTwo"])
        first, second = result
        first, second = await asyncio.gather(first, second)
    assert [await first.fetch(), await second.fetch()] == [10, 20]
    assert len(runtime.objects) == 3


async def test_checked_missing_actual_slot_fails_and_releases_real_slots(result_peer):
    remote, runtime = result_peer

    async def missing():
        call = remote.node("DeclaredThree", return_call=True, values=[1, 2])
        with pytest.raises(IndexError):
            await call
        assert call.status == "completed"
        return weakref.ref(call._state)

    state = await missing()
    await asyncio.sleep(0)
    gc.collect()
    await eventually(lambda: len(runtime.released) == 2)
    assert len(set(runtime.released)) == 2
    gc.collect()
    assert state() is None


@pytest.mark.parametrize("declared", ["DeclaredZero", "DeclaredThree", "UnknownArity"])
async def test_unchecked_fixed_unpack_uses_local_n_and_submits_without_await(result_peer, declared):
    remote, runtime = result_peer
    first, second = remote.node(declared, declared_outputs=False, values=[11, 22], blocked=True)
    assert isinstance(first, OutputFuture) and isinstance(second, OutputFuture)
    assert (first.slot, second.slot) == (0, 1)
    await runtime.started.wait()
    assert not runtime.completed
    downstream = remote.node("Echo", value=second)
    runtime.gate.set()
    assert await (await downstream).fetch() == 22
    assert await fetched((first, second)) == [11, 22]
    assert len(runtime.submissions) == 2


async def test_unchecked_missing_projected_slot_only_fails_when_used(result_peer):
    remote, runtime = result_peer
    first, missing = remote.node("DeclaredThree", declared_outputs=False, values=[7])
    assert first.slot == 0 and missing.slot == 1
    assert await (await first).fetch() == 7
    with pytest.raises(IndexError):
        await missing
    del first, missing
    gc.collect()
    await eventually(lambda: len(runtime.released) == 1)


@pytest.mark.parametrize("values", [[], [5], [5, 6]])
async def test_unchecked_await_normalizes_actual_shape_but_call_keeps_tuple(result_peer, values):
    remote, runtime = result_peer
    proxy = remote.node("DeclaredThree", declared_outputs=False, values=values, blocked=True)
    await runtime.started.wait()
    assert not runtime.completed
    runtime.gate.set()
    result = await proxy
    if not values:
        assert result is None
    elif len(values) == 1:
        assert not isinstance(result, tuple) and await result.fetch() == values[0]
    else:
        assert isinstance(result, tuple)
        assert [await ref.fetch() for ref in result] == values
    call = remote.node("DeclaredThree", return_call=True, declared_outputs=False, values=values)
    refs = await call
    assert isinstance(refs, tuple) and [await ref.fetch() for ref in refs] == values


async def test_default_single_aliases_share_release_and_keep_no_hidden_call(peer):
    remote, runtime = peer
    future = remote.node("Echo", value=9)
    first = await future
    second = await future
    assert first is second
    first.release()
    with pytest.raises(Released):
        await future
    with pytest.raises(Released):
        await second.fetch()
    await eventually(lambda: len(runtime.released) == 1)


async def test_default_one_retained_pair_slot_does_not_retain_the_other(peer):
    remote, runtime = peer
    pair = remote.node("Pair", value="keep one")
    retained = pair[1]
    del pair
    ready = await retained
    await eventually(lambda: len(runtime.released) == 1)
    assert ready.id not in runtime.released and await ready.fetch() == "keep one"
    del retained, ready
    gc.collect()
    await eventually(lambda: len(runtime.released) == 2)
    assert len(set(runtime.released)) == 2


async def test_return_call_retains_metadata_and_explicit_extra_slots(result_peer):
    remote, _ = result_peer
    call = remote.node("DeclaredTwo", return_call=True, values=[8, 9, 10])
    assert isinstance(call, Call) and isinstance(call.id, str)
    first, second = await call
    metadata = await call.metadata()
    assert metadata.call_id == call.id and metadata.prompt_id == "native-result"
    assert metadata.executed == ("result",) and metadata.ui == {"result": {"ok": True}}
    assert call.status == "completed" and await (await call.output(2)).fetch() == 10
    assert [await first.fetch(), await second.fetch()] == [8, 9]


async def test_api_option_names_can_still_be_real_node_inputs(peer):
    remote, runtime = peer
    inputs = {"return_call": "port-call", "declared_outputs": "port-declared",
              "check": "port-check", "extra_data": "port-extra",
              "inputs": "port-inputs", "node_inputs": "port-node-inputs"}
    call = remote.node("Echo", inputs, return_call=True, declared_outputs=True,
                       extra_data={"extra_pnginfo": {"label": "metadata"}})
    value, = await call
    assert await value.fetch() == inputs
    assert runtime.submissions[-1][1]["inputs"] == inputs
    assert runtime.submissions[-1][1]["extra_data"] == {"extra_pnginfo": {"label": "metadata"}}


async def test_unpublished_switch_names_are_ordinary_node_inputs(peer):
    remote, runtime = peer
    future = remote.node("Echo", check=False, use_declared_output_count=False)
    assert isinstance(future, OutputFuture)
    expected = {"check": False, "use_declared_output_count": False}
    assert await (await future).fetch() == expected
    assert runtime.submissions[-1][1]["inputs"] == expected


async def test_cold_default_requires_metadata_but_retained_call_can_start_lazily():
    runtime = ResultRuntime()
    remote = Client(transport=InProcessTransport(runtime))
    try:
        with pytest.raises(UnknownOutputLayout):
            remote.node("Echo", value="not submitted")
        assert not runtime.submissions
        call = remote.node("Echo", return_call=True, value="lazy")
        reference, = await call
        assert await reference.fetch() == "lazy"
        direct = remote.node("Echo", value="prepared")
        assert await (await direct).fetch() == "prepared"
    finally:
        await remote.close()


async def test_unknown_declaration_is_not_inferred_from_actual_results(result_peer):
    remote, _ = result_peer
    call = remote.node("UnknownArity", return_call=True, values=[10, 20])
    with pytest.raises(UnknownOutputLayout):
        await call
    assert call.status == "completed"
    assert await (await call.output(0)).fetch() == 10
    with pytest.raises(UnknownOutputLayout):
        remote.node("UnknownArity", values=[10, 20])


async def test_checked_graph_unpack_error_stays_before_submit(result_peer):
    remote, runtime = result_peer
    with pytest.raises(ValueError, match="too many values"):
        with remote.graph() as graph:
            first, second = remote.node("DeclaredThree")
    assert not runtime.submissions
    with pytest.raises(ValueError):
        remote.submit(graph)


async def test_graph_default_and_unchecked_slots_survive_submit_and_composition(result_peer):
    remote, runtime = result_peer
    with remote.graph() as graph:
        single = remote.node("Echo", value="symbolic")
        assert isinstance(single, GraphRef) and await single is single
        first, second = remote.node("Pair", value=single)
        off_first, off_second = remote.node("Echo", declared_outputs=False, value=second)
        assert all(isinstance(ref, GraphRef) for ref in (first, second, off_first, off_second))
        graph.output("value", first)
    assert not runtime.submissions
    run = remote.submit(graph, return_call=True)
    bound = await run[first]
    assert await bound.fetch() == "middle:2"
    downstream = remote.node("Echo", value=run[first])
    assert await (await downstream).fetch() == "middle:2"
    assert len(runtime.submissions) == 2
