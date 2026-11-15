from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import cloudpickle

from comani.errors import PackageConflict

PYTHON_NODE = "ComaniPython"
INPUT_NODE = "ComaniBoundInput"
COLLECT_NODE = "ComaniValidateTargets"

# Uploaded packages use ordinary Python process namespaces. Keep their files
# available as long as modules can import siblings, including after a session
# closes. A name can never be silently rebound to another artifact/version.
_PACKAGE_DIRECTORY = tempfile.TemporaryDirectory(prefix="comani-packages-")
_PACKAGE_NAMES: dict[str, str] = {}
_PACKAGE_LOCK = threading.RLock()


class Artifacts:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="comani-code-")
        self.root = Path(self.directory.name)
        self.functions = {}
        self.session = ""

    def put(self, artifact_id: str, data: str, kind: str) -> dict:
        raw = base64.b64decode(data, validate=True)
        if hashlib.sha256(raw).hexdigest() != artifact_id:
            raise ValueError("Artifact SHA256 does not match its content")
        path = self.root / artifact_id
        present = path.exists()
        if not present:
            path.mkdir()
            (path / ".payload").write_bytes(raw)
            if kind == "package":
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    for item in archive.infolist():
                        destination = (path / item.filename).resolve()
                        if path.resolve() not in destination.parents:
                            raise ValueError("Package archive path escapes its artifact")
                        if (item.external_attr >> 16) & 0o170000 == 0o120000:
                            raise ValueError("Package symlinks are not supported")
                    archive.extractall(path)
        return {"artifact_id": artifact_id, "present": present}

    def load(self, code: dict):
        version = code.get("python")
        if code["kind"] == "pickle" and version and tuple(version) != sys.version_info[:3]:
            raise ValueError(f"Pickled code requires Python {version}, runtime is {sys.version_info[:3]}")
        if code["kind"] == "pickle" and code.get("cloudpickle") and code["cloudpickle"] != cloudpickle.__version__:
            raise ValueError("Pickled code requires the same cloudpickle version as the runtime")
        key = (self.session, code["artifact_id"], code.get("entrypoint"))
        if key in self.functions:
            return self.functions[key]
        path = self.root / code["artifact_id"]
        raw = (path / ".payload").read_bytes()
        if code["kind"] == "pickle":
            function = cloudpickle.loads(raw)
        elif code["kind"] == "file":
            filename = path / "module.py"
            filename.write_bytes(raw)
            spec = importlib.util.spec_from_file_location(self._namespace(code["artifact_id"]), filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            function = module
            for part in (code.get("entrypoint") or "main").split("."):
                function = getattr(function, part)
        elif code["kind"] == "package":
            module, attribute = self._package_module(code, raw, path)
            function = module
            for part in attribute.split("."):
                function = getattr(function, part)
        else:
            raise ValueError("Unknown code artifact kind")
        if not callable(function):
            raise TypeError("Code entrypoint is not callable")
        self.functions[key] = function
        return function

    @staticmethod
    def _package_module(code, raw, path):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            roots = {item.filename.split("/", 1)[0] for item in archive.infolist() if item.filename}
        if len(roots) != 1:
            raise ValueError("A package artifact must contain one root package directory")
        root = roots.pop()
        if not root.isidentifier() or not (path / root).is_dir():
            raise ValueError("The package root must be a valid Python package name")
        if code.get("package_root", root) != root:
            raise ValueError("Package root metadata does not match the uploaded directory")
        module_name, separator, attribute = (code.get("entrypoint") or "").partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError("Package entrypoint must be module:function")
        if module_name != root and not module_name.startswith(root + "."):
            module_name = root + "." + module_name
        artifact = code["artifact_id"]
        with _PACKAGE_LOCK:
            installed = _PACKAGE_NAMES.get(root)
            if installed is not None and installed != artifact:
                raise PackageConflict(f"Package {root!r} is already bound to a different content hash; use a different package name or restart the daemon")
            if installed is None:
                if root in sys.modules or importlib.util.find_spec(root) is not None:
                    raise PackageConflict(f"Package {root!r} is already provided by the runtime environment; uploaded code cannot replace installed or imported modules")
                destination = Path(_PACKAGE_DIRECTORY.name) / artifact
                shutil.copytree(path, destination, dirs_exist_ok=True)
                _PACKAGE_NAMES[root] = artifact
                sys.path.insert(0, str(destination))
                importlib.invalidate_caches()
            return importlib.import_module(module_name), attribute

    def _namespace(self, artifact):
        return "_comani_" + hashlib.sha256(self.session.encode()).hexdigest()[:16] + "_" + artifact

    def clear_session(self, session):
        for key in list(self.functions):
            if key[0] == session:
                del self.functions[key]
        prefix = "_comani_" + hashlib.sha256(session.encode()).hexdigest()[:16] + "_"
        for name in list(sys.modules):
            if name.startswith(prefix):
                del sys.modules[name]


def unpack_layout(tree, ports):
    if isinstance(tree, dict):
        if "$port" in tree:
            return ports[tree["$port"]]
        if "$tuple" in tree:
            return tuple(unpack_layout(item, ports) for item in tree["$tuple"])
        if "$list" in tree:
            return [unpack_layout(item, ports) for item in tree["$list"]]
        if "$dict" in tree:
            return {unpack_layout(key, ports): unpack_layout(value, ports) for key, value in tree["$dict"]}
        return {key: unpack_layout(value, ports) for key, value in tree.items()}
    if isinstance(tree, list):
        return [unpack_layout(value, ports) for value in tree]
    return tree


def register_python_nodes(nodes, artifacts: Artifacts):
    from comfy_api.latest import io as node_io

    class PythonNode(node_io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return node_io.Schema(
                node_id=PYTHON_NODE,
                inputs=[node_io.String.Input("code"), node_io.String.Input("layout")],
                outputs=[node_io.AnyType.Output()],
                accept_all_inputs=True,
            )

        @classmethod
        def fingerprint_inputs(cls, **kwargs):
            return float("nan")

        @classmethod
        async def execute(cls, code, layout, **ports):
            import inspect

            function = artifacts.load(json.loads(code) if isinstance(code, str) else code)
            tree = json.loads(layout) if isinstance(layout, str) else layout
            args = unpack_layout(tree["args"], ports)
            kwargs = unpack_layout(tree["kwargs"], ports)
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return node_io.NodeOutput(result)

    class BoundInput(node_io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return node_io.Schema(node_id=INPUT_NODE, inputs=[node_io.String.Input("token")], outputs=[node_io.AnyType.Output()])

        @classmethod
        def execute(cls, token):
            from .objects import RefExpired

            raise RefExpired("A precomputed input is unavailable; it will not be recomputed")

    class Collect(node_io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return node_io.Schema(node_id=COLLECT_NODE, inputs=[], outputs=[], accept_all_inputs=True, is_output_node=True)

        @classmethod
        def execute(cls, **kwargs):
            return node_io.NodeOutput()

    for node in (PythonNode, BoundInput, Collect):
        node.GET_SCHEMA()
        nodes.NODE_CLASS_MAPPINGS[node.GET_SCHEMA().node_id] = node
