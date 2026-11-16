"""The management RPC surface shared by all namespace adapters."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager

from . import archive_plugins, bash, comfyui, git_plugins, plugins
from .models import ModelService
from .operations import ManagementError, OperationRegistry
from .providers import ProviderResolver


class ManagementService:
    def __init__(self, supervisor, *, operation_limits=None):
        self.supervisor = supervisor
        self.operations = OperationRegistry(**(operation_limits or {}))
        self.providers = ProviderResolver()
        self.models = ModelService(supervisor, self.providers)
        self._emergency_active = False
        self._control_record = contextvars.ContextVar("management_control_record", default=None)
        self._download_tool_deployment = None
        supervisor.control_barrier = self.control_barrier

    def download_tools_status(self):
        from .download_tools import aria2_status

        return {"aria2": aria2_status(self.providers),
                "deployment": self._download_tool_deployment}

    async def prepare_download_tools(self, identity):
        """Explicit daemon deployment work, using the downloader's shared owner."""
        from .download_tools import ensure_aria2

        self._download_tool_deployment = {"state": "preparing"}
        window = self.operations.window(identity)
        operation = await self.operations.begin(
            identity, "models", "prepare_download_tool", {"tool": "aria2"},
            "daemon-deployment-aria2", window["daemon_epoch"], window["window"],
            lambda context: ensure_aria2(context, self.providers,
                                         cwd=self.supervisor.config.workspace))
        record = self.operations.get(operation["operation_id"], operation["daemon_epoch"], identity)
        self._download_tool_deployment = {
            "state": "preparing", "operation_id": record.id,
            "daemon_epoch": self.operations.epoch, "namespace": record.namespace,
            "action": record.action}
        await asyncio.shield(record.task)
        self._download_tool_deployment = {
            **self._download_tool_deployment,
            "state": "ready" if record.state == "succeeded" else record.state,
            "error": record.error}
        if record.state != "succeeded":
            error = record.error or {"code": "DownloadToolInstallationFailed",
                                     "message": "aria2 deployment preparation did not complete"}
            raise ManagementError(error["code"], error["message"], error.get("details"))
        return record.result

    async def dispatch(self, method, params, identity):
        if not isinstance(params, dict):
            raise ManagementError("InvalidArgument", "RPC params must be an object")
        if method == "management_window":
            return self.operations.window(identity)
        if method == "management_read":
            return await self.read(params.get("namespace"), params.get("action"), params.get("params", {}))
        if method == "management_begin":
            namespace, action = params.get("namespace"), params.get("action")
            arguments = params.get("params", {})
            if not isinstance(arguments, dict):
                raise ManagementError("InvalidArgument", "Operation params must be an object")
            if any(key.startswith("_") for key in arguments):
                raise ManagementError("InvalidArgument", "Private adapter arguments are not part of the management API")
            self.validate_action(namespace, action)
            return await self.operations.begin(identity, namespace, action, arguments,
                params.get("key"), params.get("daemon_epoch"), params.get("window"),
                lambda context: self.run(namespace, action, arguments, context),
                admission_check=lambda: self._admission(namespace, action))
        if method in {"management_status", "management_result", "management_logs", "management_cancel"}:
            rec = self.operations.get(params.get("operation_id"), params.get("daemon_epoch"), identity)
            if method == "management_cancel":
                return await self.operations.cancel(rec)
            if method == "management_logs":
                return await self.operations.logs(rec, **{key: params[key] for key in
                    ("after", "follow", "timeout", "limit") if key in params})
            if method == "management_result" and rec.state not in {"succeeded", "failed", "cancelled"}:
                rec.changed.clear()
                try:
                    await asyncio.wait_for(rec.changed.wait(), min(max(float(params.get("timeout", 20)), 0), 25))
                except TimeoutError:
                    pass
            return self.operations.snapshot(rec)
        raise ManagementError("UnknownMethod", "Unknown management method")

    def validate_action(self, namespace, action):
        actions = {"bash": {"run"}, "comfyui": {"configure", "install", "update", "start", "stop", "restart", "prepare_runtime", "prepare_tools", "create_environment"},
                   "plugins": {"install", "update", "remove", "uninstall", "enable", "disable", "install_git", "update_git", "install_archive"},
                   "models": {"download", "remove", "snapshot"}}
        if action not in actions.get(namespace, set()):
            raise ManagementError("UnknownMethod", "Unknown management operation")

    async def read(self, namespace, action, params):
        if not isinstance(params, dict):
            raise ManagementError("InvalidArgument", "Read params must be an object")
        if namespace == "comfyui":
            if action in {"info", "config"}:
                result = await comfyui.info(self.supervisor)
                result["download_tools"] = self.download_tools_status()
                return result
            if action == "logs":
                return {"entries": list(self.supervisor.logs)}
        elif namespace == "plugins":
            if action == "backend":
                return await plugins.backend(self.supervisor)
            if action == "list":
                return await plugins.listing(self.supervisor)
            if action == "versions":
                return await plugins.versions(**params)
        elif namespace == "models":
            if action == "resolve":
                return await self.providers.resolve(**params)
            if action == "versions":
                return await self.providers.versions(**params)
            if action == "manifest":
                return await self.providers.manifest(**params)
            if action == "categories":
                return await self.models.categories()
            if action == "list":
                return await self.models.listing(**params)
        raise ManagementError("UnknownMethod", "Unknown management read")

    async def run(self, namespace, action, params, context):
        if namespace == "bash":
            return await bash.run(self.supervisor, context, **params)
        if namespace == "comfyui":
            if action in {"stop", "restart"}:
                token = self._control_record.set(context.record)
                try:
                    return await getattr(self.supervisor, action)(**params)
                finally:
                    self._control_record.reset(token)
            if action in {"prepare_runtime", "prepare_tools", "create_environment"}:
                return await getattr(comfyui, action)(self.supervisor, context, **params)
            if action in {"configure", "start", "stop", "restart"}:
                return await getattr(self.supervisor, action)(**params)
            return await comfyui.change(self.supervisor, context, action, **params)
        if namespace == "plugins":
            if action == "install_archive":
                return await archive_plugins.install(self.supervisor, context, self.providers, **params)
            if action in {"install_git", "update_git"}:
                if "_locked" in params:
                    raise ManagementError("InvalidArgument", "Invalid Git plugin argument")
                return await git_plugins.change(self.supervisor, context, action, **params)
            return await plugins.change(self.supervisor, context, action, **params)
        if namespace == "models":
            return await getattr(self.models, action)(context, **params)
        raise ManagementError("UnknownMethod", "Unknown management operation")

    @staticmethod
    def _related(namespace, action):
        return ((namespace == "comfyui" and action in {
            "configure", "create_environment", "prepare_runtime", "prepare_tools",
            "install", "update", "start", "stop", "restart"}) or namespace == "plugins")

    def _admission(self, namespace, action):
        if self._emergency_active and self._related(namespace, action):
            raise ManagementError("RuntimeControlBusy", "Emergency runtime control is interrupting related maintenance; retry after it finishes")

    @asynccontextmanager
    async def control_barrier(self, *, wait):
        if wait:
            yield []
            return
        if self._emergency_active:
            raise ManagementError("RuntimeControlBusy", "Emergency runtime control is already in progress")
        self._emergency_active = True
        current = self._control_record.get()
        interrupted = []
        try:
            records = [record for record in self.operations._records.values()
                       if record is not current and record.state not in {"succeeded", "failed", "cancelled"}
                       and self._related(record.namespace, record.action)]
            # Cancel queued and running related work before releasing the
            # admission barrier. Each task owns and reaps only its process group.
            await asyncio.gather(*(self.operations.cancel(record) for record in records))
            for record in records:
                interrupted.append({"operation_id": record.id, "daemon_epoch": self.operations.epoch,
                    "namespace": record.namespace, "action": record.action, "state": record.state,
                    "partial_changes": record.result.get("partial_changes", "unknown")
                    if isinstance(record.result, dict) else False if record.started is None else "unknown"})
            if current is not None:
                current.result = {"interrupted_operations": interrupted,
                                  "partial_changes": bool(interrupted)}
            yield interrupted
        finally:
            if current is not None and isinstance(current.result, dict):
                current.result["runtime_state"] = self.supervisor.state
            self._emergency_active = False

    async def close(self):
        await self.operations.close()
