from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from comani.errors import PackageConflict

from .objects import CallFailed
from .python_node import INPUT_NODE, Artifacts, register_python_nodes
from .startup import cache_configuration, initialize


class NativeAdapter:
    """The sole owner of a native executor; run() is called by one worker."""

    def __init__(self, comfy_root, *, cpu=False, custom_nodes=True, event=None, comfy_args=()):
        self.root = Path(comfy_root).resolve()
        self.cpu = cpu
        self.custom_nodes = custom_nodes
        self.comfy_args = tuple(comfy_args)
        self.event = event or (lambda *_: None)
        self.artifacts = Artifacts()
        self.bound = {}
        self.capture = None
        self.executed = set()
        self.booted = False

    def boot(self):
        if self.booted:
            return
        if not (self.root / "execution.py").is_file():
            raise ValueError("comfy_root must point to a ComfyUI checkout")
        if self.comfy_args and "comfy.cli_args" in sys.modules:
            raise RuntimeError("Native ComfyUI arguments require a fresh process before importing comfy.cli_args")
        sys.path.insert(0, str(self.root))
        original_argv = sys.argv
        sys.argv = ["comani-runtime", "--disable-assets"]
        if self.cpu:
            sys.argv.append("--cpu")
        if not self.custom_nodes:
            sys.argv.append("--disable-all-custom-nodes")
        sys.argv.extend(self.comfy_args)
        try:
            import comfy.options

            comfy.options.enable_args_parsing()
            native, args = initialize(self.root, self.comfy_args, custom_nodes=self.custom_nodes)
            self.nodes = native.nodes
            self.execution = native.execution
            native_server = native.server
            from app.assets.manager import NoAssets

            event_loop = asyncio.new_event_loop()
            self.server = native_server.PromptServer(event_loop, NoAssets(args))
            self.server.client_id = "comani"
            self.server.send_sync = self._event
            native.hook_breaker_ac10a0.save_functions()
            try:
                event_loop.run_until_complete(self.nodes.init_extra_nodes(
                    init_custom_nodes=self.custom_nodes and (not args.disable_all_custom_nodes or bool(args.whitelist_custom_nodes)),
                    init_api_nodes=not args.disable_partner_nodes,
                ))
            finally:
                native.hook_breaker_ac10a0.restore_functions()
                event_loop.close()
            disabled = native.governance.load_disabled_nodes(args.disabled_nodes_config) if args.disabled_nodes_config else set()
            native.governance.apply_disabled_nodes(disabled)
            native.comfy.model_management.set_cudnn_benchmark()
            register_python_nodes(self.nodes, self.artifacts)
            import comfy.model_management

            cache_type, cache_args = cache_configuration(args, self.execution, comfy.model_management.total_ram)
            self.executor = self.execution.PromptExecutor(
                self.server,
                cache_type=cache_type, cache_args=cache_args,
            )
            self._install_cache_hooks()
            self._install_progress_hook()
            self.booted = True
        finally:
            sys.argv = original_argv

    def _event(self, kind, data, sid=None):
        if kind == "executing" and isinstance(data, dict) and data.get("node"):
            self.executed.add(data["node"])
        self.event(kind, data)

    def _install_progress_hook(self):
        import comfy.model_management
        import comfy.utils
        from comfy_execution.progress import get_progress_state
        from comfy_execution.utils import get_executing_context
        from protocol import BinaryEventTypes

        self.previous_progress_hook = comfy.utils.PROGRESS_BAR_HOOK

        def hook(value, total, preview_image, prompt_id=None, node_id=None):
            context = get_executing_context()
            if context is not None:
                prompt_id = prompt_id if prompt_id is not None else context.prompt_id
                node_id = node_id if node_id is not None else context.node_id
            prompt_id = prompt_id if prompt_id is not None else getattr(self, "prompt_id", None)
            node_id = node_id if node_id is not None else self.server.last_node_id
            comfy.model_management.throw_exception_if_processing_interrupted()
            get_progress_state().update_progress(node_id, value, total, preview_image)
            self.server.send_sync("progress", {"value": value, "max": total, "prompt_id": prompt_id, "node": node_id})
            if preview_image is not None:
                self.server.send_sync(BinaryEventTypes.UNENCODED_PREVIEW_IMAGE, preview_image)

        self.progress_hook = hook
        comfy.utils.set_progress_bar_global_hook(hook)

    def _install_cache_hooks(self):
        from comfy_api.latest._caching import CacheProvider, CacheValue
        from comfy_execution.cache_provider import register_cache_provider

        adapter = self

        class InputProvider(CacheProvider):
            def should_cache(self, context, value=None):
                return context.class_type == INPUT_NODE

            async def on_lookup(self, context):
                data = adapter.bound.get(context.node_id)
                return None if data is None else CacheValue(outputs=[data])

            async def on_store(self, context, value):
                pass

        self.provider = InputProvider()
        register_cache_provider(self.provider)
        outputs = self.executor.caches.outputs
        native_set, native_get = outputs.set, outputs.get

        async def capture_set(node_id, entry):
            await native_set(node_id, entry)
            self._version(node_id, entry, changed=True)
            self._capture(node_id, entry)

        async def capture_get(node_id):
            owner, key = self._cache_location(node_id)
            before = owner.cache.get(key)
            entry = await native_get(node_id)
            if entry is not None:
                self._version(node_id, entry, changed=before is not entry)
                self._capture(node_id, entry)
            return entry

        outputs.set = capture_set
        outputs.get = capture_get

    def _cache_location(self, node_id):
        cache = self.executor.caches.outputs
        owner = cache._get_cache_for(node_id) if hasattr(cache, "_get_cache_for") else cache
        return owner, owner.cache_key_set.get_data_key(node_id)

    def _version(self, node_id, entry, *, changed):
        owner, key = self._cache_location(node_id)
        if not hasattr(owner, "_comani_entry_versions"):
            owner._comani_entry_versions = {}
        if changed or key not in owner._comani_entry_versions:
            owner._comani_entry_versions[key] = uuid.uuid4().hex

    def _capture(self, node_id, entry):
        if self.capture is not None and node_id not in self.bound:
            owner, key = self._cache_location(node_id)
            self.capture(node_id, entry, owner, key)

    def run(self, graph: dict, targets: list[str], bindings: dict, capture, *, prompt_id=None, session_id="", extra_data=None):
        self.boot()
        self.bound = bindings
        self.capture = capture
        self.artifacts.session = session_id
        self.executed = set()
        prompt_id = prompt_id or str(uuid.uuid4())
        self.prompt_id = prompt_id
        self.executor.success = True
        try:
            asyncio.run(self._execute(graph, targets, prompt_id, extra_data or {}))
            if not self.executor.success:
                errors = [data for kind, data in self.executor.status_messages if kind in ("execution_error", "execution_interrupted")]
                detail = errors[-1] if errors else {}
                if detail.get("exception_type", "").endswith(".PackageConflict"):
                    raise PackageConflict(detail.get("exception_message", "Uploaded package conflicts with the runtime namespace"))
                raise CallFailed(detail.get("exception_message", "Native execution failed"))
            return {"history": self.executor.history_result, "executed": sorted(self.executed), "prompt_id": prompt_id}
        finally:
            self.bound = {}
            self.capture = None
            owner = self.executor.caches.outputs
            if hasattr(owner, "_comani_entry_versions"):
                owner._comani_entry_versions = {key: version for key, version in owner._comani_entry_versions.items() if key in owner.cache}

    async def _execute(self, graph, targets, prompt_id, extra_data):
        # validate_prompt discovers only OUTPUT_NODE roots. Eager calls and
        # explicit graph targets also include ordinary nodes: use its native
        # input/loop validators on those exact targets instead of a fake root.
        validated = {}
        for target in targets:
            valid, errors, _ = await self.execution.validate_inputs(prompt_id, graph, target, validated)
            if not valid:
                all_errors = {name: value[1] for name, value in validated.items() if not value[0]}
                raise CallFailed(json.dumps(all_errors or errors, default=str))
        from comfy_execution.validation import validate_loops

        starts, ends = set(), set()
        for node_id, node in graph.items():
            definition = self.nodes.NODE_CLASS_MAPPINGS[node["class_type"]]
            if issubclass(definition, self.execution._ComfyNodeInternal):
                boundary = definition.GET_SCHEMA().loop_boundary
                if boundary == "start":
                    starts.add(node_id)
                elif boundary == "end":
                    ends.add(node_id)
        validate_loops(graph, set(targets), validated, starts, ends)
        await self.executor.execute_async(graph, prompt_id, {"client_id": "comani", **extra_data}, targets)

    def node_types(self):
        self.boot()
        result = {}
        for name, node in self.nodes.NODE_CLASS_MAPPINGS.items():
            try:
                raw = node.RETURN_TYPES
                fixed = type(raw) in (tuple, list)
                layout = "fixed" if fixed else "unknown"
                if isinstance(raw, (tuple, list)) and not fixed:
                    layout = "dynamic"
                schema = getattr(node, "SCHEMA", None)
                if schema is not None and any(isinstance(output, self.execution._io.DynamicOutput)
                                              for output in schema.outputs):
                    fixed = False
                    layout = "dynamic"
                result[name] = {"outputs": list(raw), "output_layout": layout,
                                "output_count": len(raw) if fixed else None,
                                "output_node": bool(getattr(node, "OUTPUT_NODE", False)),
                                "output_names": list(getattr(node, "RETURN_NAMES", ()) or ()),
                                "output_is_list": list(getattr(node, "OUTPUT_IS_LIST", ()) or ())}
            except Exception:
                result[name] = {"outputs": [], "output_layout": "unknown", "output_count": None}
        return result

    def close(self):
        if self.booted:
            import comfy.utils
            from comfy_execution.cache_provider import unregister_cache_provider

            unregister_cache_provider(self.provider)
            if comfy.utils.PROGRESS_BAR_HOOK is self.progress_hook:
                comfy.utils.set_progress_bar_global_hook(self.previous_progress_hook)
        self.artifacts.directory.cleanup()


def native_python_inputs(inputs: dict) -> dict:
    def linked(value):
        return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], (int, float))

    if "code" in inputs and not isinstance(inputs["code"], str) and not linked(inputs["code"]):
        inputs = dict(inputs, code=json.dumps(inputs["code"]))
    if "layout" in inputs and not isinstance(inputs["layout"], str) and not linked(inputs["layout"]):
        inputs = dict(inputs, layout=json.dumps(inputs["layout"]))
    return inputs
