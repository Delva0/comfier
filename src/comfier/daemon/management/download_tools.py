"""Ensure the daemon's aria2 binary through its native package manager."""

from __future__ import annotations

import asyncio
import copy
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from .operations import ManagementError
from .processes import run_process
from .providers import safe_url


class _QuietToolContext:
    def __init__(self, context):
        # Probe output has its own small temporary bound. Exhausted user logs
        # must not make a working executable look as though it has no version.
        self.record = SimpleNamespace(id=context.record.id, capture_remaining=4096)

    def log(self, *args, **kwargs):
        pass

    def partial(self, value):
        pass


def _state(owner):
    state = getattr(owner, "_aria2_setup_state", None)
    if state is None:
        state = {"lock": asyncio.Lock(), "generation": 0, "failure": None,
                 "status": {"state": "not_checked"}}
        owner._aria2_setup_state = state
    return state


def aria2_status(owner):
    state = getattr(owner, "_aria2_setup_state", None)
    return copy.deepcopy(state["status"] if state else {"state": "not_checked"})


def _diagnostic(command):
    value = (command.get("stderr", "") + "\n" + command.get("stdout", ""))[-2000:]
    return re.sub(r"https?://[^\s\"'<>]+", lambda match: safe_url(match[0]), value).strip()


def _failure(phase, installer=None, command=None, message=None):
    details = {"transport": "aria2", "tool": "aria2c", "phase": phase,
               "installer": installer,
               "hint": 'Repair daemon host package access/permissions or explicitly select transport="http" or "hf".'}
    if command is not None:
        details.update(exit_code=command.get("exit_code"), diagnostic=_diagnostic(command))
    return ManagementError("DownloadToolInstallationFailed",
                           message or "Automatic aria2 preparation failed; no model was downloaded", details)


async def _version(context, binary, cwd, timeout):
    try:
        command = await run_process(_QuietToolContext(context), [binary, "--no-conf=true", "--no-netrc=true", "--version"],
                                    cwd=cwd, timeout=min(timeout, 30), output_limit=4096)
    except (OSError, ManagementError):
        return None
    match = re.search(r"(?m)^aria2 version ([^\s]+)", command.get("stdout", ""))
    return match[1] if command["exit_code"] == 0 and match else None


def _installation_plan():
    for name, commands in (
        ("apt-get", (("apt_update", ["update"]),
                     ("apt_install", ["install", "--yes", "--no-install-recommends", "aria2"]))),
        ("apk", (("apk_install", ["add", "--no-cache", "aria2"]),)),
        ("dnf", (("dnf_install", ["install", "--assumeyes", "aria2"]),)),
        ("yum", (("yum_install", ["install", "--assumeyes", "aria2"]),)),
    ):
        binary = shutil.which(name)
        if binary:
            return name, [(phase, [binary, *arguments]) for phase, arguments in commands]
    raise _failure("select_installer", message="No supported host package manager can install aria2c")


async def ensure_aria2(context, owner, *, cwd, timeout=None):
    """Deployment and download share one preparation attempt per daemon owner.

    A new call after a failed attempt may retry. Calls already waiting on that
    attempt receive the same explicit failure instead of repeating apt. Other
    daemon processes retain their package manager's native locking semantics.
    """
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ManagementError("InvalidArgument", "Timeout must be positive")
    limit = min(timeout, 300) if timeout is not None else 300
    state = _state(owner)
    observed = state["generation"]
    async with state["lock"]:
        binary = shutil.which("aria2c")
        state["status"] = {"state": "checking", "stage": "verify_existing"}
        if binary:
            version = await _version(context, binary, cwd, limit)
            if version:
                result = {"binary": binary, "version": version, "installed": False, "installer": None}
                state["status"] = {"state": "ready", **result}
                context.progress(download_tool=result)
                return result
        if state["generation"] != observed and state["failure"] is not None:
            error = state["failure"]
            state["status"] = {"state": "failed", "error": copy.deepcopy(error)}
            raise ManagementError(error["code"], error["message"], {**error["details"], "coalesced": True})
        installer, phase, changed = None, "prepare_workspace", False
        try:
            if os.name != "posix":
                raise _failure("platform", message="Automatic aria2 setup requires a supported Linux daemon host")
            await asyncio.to_thread(Path(cwd).mkdir, parents=True, exist_ok=True)
            phase = "select_installer"
            installer, commands = _installation_plan()
            prefix = []
            if os.geteuid() != 0:
                sudo = shutil.which("sudo")
                if not sudo:
                    raise _failure("permissions", installer,
                                   message="aria2c is missing and the daemon has neither root nor noninteractive sudo")
                prefix = [sudo, "-n", "--", "env", "DEBIAN_FRONTEND=noninteractive",
                          "APT_LISTCHANGES_FRONTEND=none"]
            for phase, command in commands:
                state["status"] = {"state": "installing", "stage": phase, "installer": installer}
                context.progress(phase="installing_download_tool", transport="aria2",
                                 download_tool_phase=phase, download_tool_installer=installer)
                changed = True
                result = await run_process(_QuietToolContext(context), [*prefix, *command], cwd=cwd,
                                           env={"DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"},
                                           timeout=limit, output_limit=4096)
                if result["exit_code"]:
                    raise _failure(phase, installer, result)
            phase = "verify_installed"
            binary = shutil.which("aria2c")
            version = await _version(context, binary, cwd, limit) if binary else None
            if not version:
                raise _failure(phase, installer, message="Installer finished but aria2c did not pass its version probe")
            result = {"binary": binary, "version": version, "installed": True, "installer": installer}
            state["generation"] += 1
            state["failure"] = None
            state["status"] = {"state": "ready", **result}
            context.progress(download_tool=result)
            return result
        except asyncio.CancelledError:
            error = _failure(phase, installer, message="aria2 preparation was cancelled; retry preparation before downloading")
            state["generation"] += 1
            state["failure"] = {"code": error.code, "message": str(error), "details": error.details}
            state["status"] = {"state": "cancelled", "stage": phase, "error": copy.deepcopy(state["failure"])}
            context.partial({"download_tool": aria2_status(owner), "partial_changes": changed})
            raise
        except (OSError, ManagementError) as cause:
            error = cause if isinstance(cause, ManagementError) and cause.code == "DownloadToolInstallationFailed" else _failure(phase, installer)
            state["generation"] += 1
            state["failure"] = {"code": error.code, "message": str(error), "details": error.details}
            state["status"] = {"state": "failed", "stage": error.details["phase"], "error": copy.deepcopy(state["failure"])}
            context.partial({"download_tool": aria2_status(owner), "partial_changes": changed})
            raise error from None
