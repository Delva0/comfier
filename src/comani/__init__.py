"""A small eager SDK for a ComfyUI-backed daemon."""

from typing import Any, Coroutine

from . import bash, comfyui, files, models, operations, plugins
from ._context import current_client as _current_client
from ._context import invoke_async as _invoke_async
from ._graph import Graph, GraphInput, GraphRef
from .client import _NODE_INPUTS, Client
from .errors import (
    CallExpired,
    CodeCompatibilityError,
    ComaniError,
    CommandError,
    DaemonRestarted,
    Expired,
    FileTransferBusy,
    FileTransferExpired,
    InvalidReference,
    LogExpired,
    NotProduced,
    NotSubmitted,
    OperationCancelled,
    OperationError,
    OperationExpired,
    OperationTimeout,
    OperationUnknown,
    PackageConflict,
    Released,
    RemoteError,
    SessionClosed,
    SessionExpired,
    Skipped,
    SubmissionKeyConflict,
    UnknownOutputLayout,
)
from .files import DownloadedFile, FileRoot, Files, RemoteFile
from .management import (
    Bash,
    ComfyUI,
    ComfyUIInfo,
    CommandResult,
    LogEntry,
    LogPage,
    ManagementMetadata,
    ManagementOperation,
    ModelArtifact,
    ModelManifest,
    Models,
    ModelSnapshot,
    ModelSource,
    OperationHandle,
    Operations,
    OperationStatus,
    PluginChanges,
    PluginInfo,
    Plugins,
)
from .refs import Call, CallMetadata, OutputFuture, RemoteRef
from .transport import HTTPTransport, InProcessTransport


def node(node_type, node_inputs=_NODE_INPUTS, /, *, return_call=False,
         declared_outputs=True, **inputs):
    """Submit eagerly, or capture in the current Client's graph."""
    return _current_client().node(node_type, node_inputs, return_call=return_call,
                                 declared_outputs=declared_outputs, **inputs)


def python(function=None, /, *, code=None, source=None, entry=None, inputs=None,
           args=(), return_call=False) -> Call | OutputFuture | GraphRef:
    """Submit one remote Python value; choose callable, code text, or source path."""
    return _current_client().python(function, code=code, source=source, entry=entry,
                                   inputs=inputs, args=args, return_call=return_call)


def graph(prompt=None, /, *, outputs=None, targets=None) -> Graph:
    """Create a capture, or import a raw native prompt with explicit selections."""
    return _current_client().graph(prompt, outputs=outputs, targets=targets)


def submit(graph, /, *, inputs=None, extra_data=None,
           return_call=False) -> Call | OutputFuture | GraphRef | tuple | None:
    """Submit eagerly; return 0/1/N exports, or a Call with return_call=True.

    Export futures wait for the entire workflow. Use an explicit Call for
    native UI files, execution metadata, or early run[graph_ref] observation.
    """
    return _current_client().submit(graph, inputs=inputs, extra_data=extra_data,
                                    return_call=return_call)


def start() -> Coroutine[Any, Any, Client]:
    return _invoke_async(("start",))


def close(*, wait=True) -> Coroutine[Any, Any, None]:
    return _invoke_async(("close",), wait=wait)


__all__ = [
    "node", "python", "graph", "submit", "start", "close",
    "files", "bash", "comfyui", "models", "plugins", "operations",
    "Client", "Call", "OutputFuture", "RemoteRef", "Graph", "GraphInput", "GraphRef",
    "HTTPTransport", "InProcessTransport", "ComaniError", "RemoteError", "SessionClosed",
    "InvalidReference", "NotSubmitted", "Expired", "Released", "NotProduced", "Skipped",
    "UnknownOutputLayout", "CodeCompatibilityError", "PackageConflict",
    "Files", "FileRoot", "RemoteFile", "DownloadedFile",
    "FileTransferExpired", "FileTransferBusy",
    "Bash", "ComfyUI", "ComfyUIInfo", "Plugins", "Models", "Operations",
    "ManagementOperation", "OperationHandle", "OperationStatus", "CommandResult",
    "ManagementMetadata", "PluginInfo", "PluginChanges", "ModelArtifact", "ModelSource",
    "LogEntry", "LogPage", "OperationError", "OperationCancelled", "OperationTimeout",
    "OperationUnknown", "OperationExpired", "DaemonRestarted", "LogExpired", "CommandError",
    "SubmissionKeyConflict", "CallExpired",
    "SessionExpired",
    "CallMetadata",
    "ModelManifest", "ModelSnapshot",
]
