"""Explicit native workflow capture, not a Python-program compiler."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from .errors import InvalidReference, NotSubmitted, UnknownOutputLayout
from .protocol import encode_tree
from .refs import Call

_receiver = contextvars.ContextVar("comani_graph_receiver", default=None)


@dataclass(frozen=True, eq=False)
class GraphInput:
    graph: object
    name: str

    def __bool__(self):
        raise TypeError("A graph input cannot drive local Python control flow")


@dataclass(frozen=True, eq=False)
class GraphRef:
    graph: object
    node_id: str
    slot: int

    def __bool__(self):
        raise TypeError("A graph reference cannot drive local Python control flow")

    def fetch(self):
        raise NotSubmitted("Bind the graph reference to a run before fetching")

    def __reduce__(self):
        raise TypeError("Pass graph references through node inputs, not pickled objects")


class _GraphCall(Call):
    def __init__(self, graph, node_id, output_count, *, names=None, refs=None, targets=None):
        self.graph = graph
        self.node_id = node_id
        self.output_count = output_count
        self.names = names or {}
        self.refs = refs
        self.target_ids = targets or ([node_id] if node_id is not None else [])

    @property
    def id(self):
        return self.node_id

    @property
    def status(self):
        return "captured"

    def output(self, selector=0):
        if isinstance(selector, str):
            selector = self.names[selector]
        if not isinstance(selector, int) or isinstance(selector, bool) or selector < 0:
            raise IndexError("Output slots must be nonnegative integers")
        if self.refs is not None and selector >= len(self.refs):
            raise IndexError(f"Output {selector} is outside the declared output layout")
        return self.refs[selector] if self.refs is not None else GraphRef(self.graph, self.node_id, selector)

    def __await__(self):
        if _receiver.get() is not self.graph:
            raise NotSubmitted("A captured call only unwraps inside its graph; use run[graph_ref] after submit")
        if self.output_count is None:
            raise UnknownOutputLayout("This node has no static output layout; use call.output(slot)")
        if False:
            yield
        return tuple(self.output(i) for i in range(self.output_count))


class Graph:
    def __init__(self, client):
        self.client = client
        self.nodes = {}
        self.inputs = {}
        self.outputs = {}
        self.targets = []
        self.artifacts = {}
        self.sealed = False
        self.native = False
        self._token = None
        self._failed = False
        self._automatic_targets = []

    def __enter__(self):
        if self.sealed or self._token is not None:
            raise ValueError("A graph capture cannot be entered twice")
        self._token = _receiver.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        _receiver.reset(self._token)
        self._token = None
        self.sealed = True
        self._failed = exc_type is not None

    def input(self, name, *, type=None):
        if self.sealed:
            raise ValueError("Graph is sealed")
        if name in self.inputs:
            raise ValueError(f"Duplicate graph input {name!r}")
        self.inputs[name] = {"type": type}
        return GraphInput(self, name)

    def output(self, name, source):
        if self.sealed:
            raise ValueError("Graph is sealed")
        self._validate_port(source)
        if not isinstance(source, GraphRef):
            raise TypeError("Workflow outputs must be node output references")
        if name in self.outputs:
            raise ValueError(f"Duplicate workflow output {name!r}")
        self.outputs[name] = source

    def target(self, call):
        if self.sealed:
            raise ValueError("Graph is sealed")
        if isinstance(call, _GraphCall) and call.graph is self:
            ids = call.target_ids
        elif isinstance(call, GraphRef) and call.graph is self:
            ids = [call.node_id]
        else:
            raise InvalidReference("Execution targets must belong to this graph")
        for node_id in ids:
            if node_id not in self.targets:
                self.targets.append(node_id)

    def _validate_port(self, value):
        if isinstance(value, (GraphRef, GraphInput)) and value.graph is not self:
            raise InvalidReference("A graph port must be bound when composing another graph")
        if isinstance(value, (list, tuple)):
            for item in value:
                self._validate_port(item)
        elif isinstance(value, dict):
            for item in value.values():
                self._validate_port(item)

    def add(self, node_type, inputs, artifacts=()):
        if self.sealed:
            raise ValueError("Graph is sealed")
        self._validate_port(inputs)
        node_id = str(len(self.nodes) + 1)
        self.nodes[node_id] = {"class_type": node_type, "inputs": inputs}
        for artifact in artifacts:
            self.artifacts[artifact.artifact_id] = artifact
        schema = self.client._nodes.get(node_type, {})
        count = self.client._output_count(node_type)
        if schema.get("output_node", schema.get("is_output_node", False)):
            self._automatic_targets.append(node_id)
        return _GraphCall(self, node_id, count)

    def compose(self, graph, bindings):
        if not graph.sealed or graph._failed:
            raise ValueError("Only successfully sealed graphs can be composed")
        missing = set(graph.inputs) - set(bindings)
        extra = set(bindings) - set(graph.inputs)
        if missing or extra:
            raise ValueError(f"Invalid workflow bindings: missing={sorted(missing)}, extra={sorted(extra)}")
        remap = {old: str(len(self.nodes) + i + 1) for i, old in enumerate(graph.nodes)}

        def replace(value):
            if isinstance(value, GraphInput) and value.graph is graph:
                return bindings[value.name]
            if isinstance(value, GraphRef) and value.graph is graph:
                return GraphRef(self, remap[value.node_id], value.slot)
            if isinstance(value, list):
                if graph.native and len(value) == 2 and str(value[0]) in remap and isinstance(value[1], int):
                    return GraphRef(self, remap[str(value[0])], value[1])
                return [replace(v) for v in value]
            if isinstance(value, tuple):
                return tuple(replace(v) for v in value)
            if isinstance(value, dict):
                return {k: replace(v) for k, v in value.items()}
            return value

        for old, node in graph.nodes.items():
            inputs = replace(node["inputs"])
            self._validate_port(inputs)
            self.nodes[remap[old]] = {"class_type": node["class_type"], "inputs": inputs}
        self.artifacts.update(graph.artifacts)
        refs = [replace(ref) for ref in graph.outputs.values()]
        targets = list(dict.fromkeys([remap[n] for n in (graph.targets or graph._automatic_targets)]
                                     + [ref.node_id for ref in refs]))
        self._automatic_targets.extend(n for n in targets if n not in self._automatic_targets)
        return _GraphCall(self, None, len(refs), names={n: i for i, n in enumerate(graph.outputs)},
                          refs=refs, targets=targets or [r.node_id for r in refs])

    def to_wire(self, client):
        if not self.sealed or self._failed:
            raise ValueError("Submit a successfully sealed graph")

        def special(value):
            self._validate_port(value)
            if isinstance(value, GraphRef):
                return {"$graph_ref": {"node_id": value.node_id, "slot": value.slot}}
            if isinstance(value, GraphInput):
                return {"$input": value.name}
            return client._encode_special(value)

        nodes = self.nodes if self.native else {
            key: {"class_type": node["class_type"],
                  "inputs": {name: encode_tree(value, special) for name, value in node["inputs"].items()}}
            for key, node in self.nodes.items()
        }
        return {"nodes": nodes, "inputs": self.inputs,
                "outputs": [{"name": name, "source": special(ref)} for name, ref in self.outputs.items()],
                "targets": list(dict.fromkeys(list(self.targets or self._automatic_targets)
                                               + [ref.node_id for ref in self.outputs.values()])),
                "native": self.native}
