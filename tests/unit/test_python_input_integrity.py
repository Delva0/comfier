"""Python arguments preserve object graphs without pickling SDK references."""

import json
import sys
import threading
from types import ModuleType, SimpleNamespace

import cloudpickle
import pytest

from comfier import (
    Client,
    CodeCompatibilityError,
    InProcessTransport,
    InvalidReference,
    RemoteError,
)
from comfier.protocol import lift_arguments, restore_arguments

from .conftest import FakeRuntime


async def test_python_aliases_cycles_and_cross_argument_identity(peer):
    remote, _ = peer
    shared = {"values": [1, 2]}
    cycle = {"shared": shared}
    cycle["self"] = cycle
    result = remote.python(
        lambda first, *, second, recursive: (
            first is second, recursive["self"] is recursive, recursive["shared"] is first
        ), args=(shared,), inputs={"second": shared, "recursive": cycle},
    )
    assert await (await result).fetch() == (True, True, True)


async def test_dynamic_callable_and_class_inputs_use_cloudpickle(peer):
    remote, _ = peer
    captured = 7
    def function(number):
        return number + captured

    class Value:
        def answer(self):
            return function(3)

    result = remote.python(lambda value, function: (value.answer(), function(4)),
                           inputs={"value": Value(), "function": function})
    assert await (await result).fetch() == (10, 11)


async def test_pending_reference_in_object_graph_stays_native_and_keeps_identity(peer):
    remote, runtime = peer
    producer = remote.node("Block", value={"answer": 42})
    wrapper = SimpleNamespace(first=producer, second=producer)
    wrapper.self = wrapper
    result = remote.python(
        lambda value: (value.self is value, value.first is value.second, value.first["answer"]),
        inputs={"value": wrapper},
    )
    runtime.gate.set()
    assert await (await result).fetch() == (True, True, 42)
    spec = runtime.submissions[-1][1]
    json.dumps(spec)
    assert spec["inputs"]["v0"]["$ref"]["call_id"] == producer._cell.state.call_id
    assert len([name for name in spec["inputs"] if name.startswith("v")]) == 1


async def test_reference_in_dynamic_input_closure_is_not_pickled(peer):
    remote, _ = peer
    producer = remote.node("Echo", value=9)
    def function():
        return producer
    result = remote.python(lambda function: function() + 1, inputs={"function": function})
    assert await (await result).fetch() == 10


async def test_nested_object_foreign_reference_keeps_namespace_check(peer):
    remote, _ = peer
    async with Client(transport=InProcessTransport(FakeRuntime())) as other:
        producer = other.node("Echo", value=7)
        with pytest.raises(InvalidReference):
            remote.python(lambda value: value, inputs={"value": SimpleNamespace(ref=producer)})


async def test_source_with_ordinary_inputs_does_not_require_identical_python(peer):
    remote, runtime = peer
    remote.handshake["python"] = [3, 12, 99]
    remote.handshake["cloudpickle"] = "a-different-version"
    # This fake executes uploaded bytes as callable artifacts. Inspect the
    # admitted spec rather than pretending to execute a source file remotely.
    original = runtime.execute

    async def execute(spec, done):
        done.set_result({"outputs": [runtime.info(42)]})

    runtime.execute = execute
    try:
        result = remote.python(code="def main(value):\n    return value\n", entry="main",
                               inputs={"value": {"nested": [1, 2, 3]}})
        assert await (await result).fetch() == 42
    finally:
        runtime.execute = original


async def test_dynamic_source_inputs_require_matching_versions_before_submit(peer):
    remote, runtime = peer
    remote.handshake["python"] = [3, 12, 99]
    result = remote.python(code="def main(function):\n    return function()\n", entry="main",
                           inputs={"function": lambda: 42})
    with pytest.raises(CodeCompatibilityError, match="Dynamic Python input"):
        await result
    assert not runtime.submissions


@pytest.mark.parametrize("capability", [None, True, 2])
async def test_unsupported_python_layout_capability_fails_before_submission(peer, capability):
    remote, runtime = peer
    remote.handshake["capabilities"]["python_inputs"] = capability
    result = remote.python(lambda: 42)
    with pytest.raises(RemoteError, match="lossless Python inputs"):
        await result
    assert not runtime.submissions


def test_unsupported_resource_has_argument_context_and_underlying_reason():
    with pytest.raises(TypeError, match="Cannot serialize Python args/inputs:.*lock"):
        lift_arguments((), {"value": threading.Lock()})


def test_daemon_still_restores_legacy_python_layout():
    layout = {"args": {"$tuple": [{"$port": "v0"}]},
              "kwargs": {"$dict": [["option", {"$port": "v1"}]]}}
    assert restore_arguments(layout, {"v0": 3, "v1": False}) == ((3,), {"option": False})


def test_importable_class_and_builtin_type_inputs_keep_standard_pickle_compatibility():
    value = SimpleNamespace(answer=42)
    value.self = value
    layout, ports = lift_arguments((value, type(None)), {})
    assert "cloudpickle" not in layout.descriptor
    args, _ = restore_arguments(layout, ports)
    assert args[0].answer == 42 and args[0].self is args[0]
    assert args[1] is type(None)


def test_registered_by_value_module_keeps_function_and_class_code(monkeypatch):
    module = ModuleType("input_integrity_registered_by_value")
    exec("def answer(number):\n    return number + 7\n"
         "class Value:\n    def answer(self, number):\n        return answer(number)\n", module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    cloudpickle.register_pickle_by_value(module)
    try:
        layout, ports = lift_arguments((module.Value(), module.answer), {})
        assert layout.descriptor["cloudpickle"] == cloudpickle.__version__
        monkeypatch.delitem(sys.modules, module.__name__)
        args, _ = restore_arguments(layout, ports)
        assert args[0].answer(3) == 10 and args[1](4) == 11
    finally:
        cloudpickle.unregister_pickle_by_value(module)
