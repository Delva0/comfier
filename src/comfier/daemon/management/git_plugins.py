"""Explicit Git plugin preparation at a resolved commit, without Manager guesses."""

from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from comfier.files import _io

from .operations import ManagementError
from .plugins import custom_node_roots, listing, package_id
from .processes import run_process
from .tools import dependency_changes, git_fact, python_metadata, tool_env


def repository_url(value):
    if not isinstance(value, str) or not value or value.startswith("-") or any(char in value for char in "\0\n\r"):
        raise ManagementError("InvalidArgument", "Expected a repository URL or absolute local repository path")
    if Path(value).is_absolute():
        return value
    if re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./-]+", value):
        return value
    parts = urlsplit(value)
    if parts.scheme not in {"https", "ssh"} or not parts.hostname or parts.password or parts.query or parts.fragment or (parts.scheme == "https" and parts.username):
        raise ManagementError("InvalidArgument", "Git URLs must use HTTPS/SSH and external credentials")
    return value


async def _step(supervisor, context, argv, cwd, steps, env, timeout):
    result = await run_process(context, argv, cwd=cwd, env=env, timeout=timeout)
    steps.append(result)
    if result["exit_code"]:
        raise ManagementError("PluginStepFailed", "A Git/plugin dependency step failed", {"step": len(steps), "exit_code": result["exit_code"]})
    return result


async def _resolved(supervisor, context, repository, commit, cwd, steps, env, timeout):
    if commit is not None:
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            raise ManagementError("InvalidArgument", "Git commit must be a complete hexadecimal object ID")
        return commit.lower()
    command = await _step(supervisor, context, ["git", "ls-remote", "--exit-code", repository, "HEAD"],
                          cwd, steps, env, timeout)
    lines = command["stdout"].splitlines()
    if len(lines) != 1 or not re.fullmatch(r"[0-9a-f]{40,64}\s+HEAD", lines[0]):
        raise ManagementError("UnverifiablePluginTarget", "Git did not resolve a unique remote HEAD")
    return lines[0].split()[0]


async def change(supervisor, context, action, *, repository=None, package=None, commit=None,
                 name=None, no_deps=False, timeout=None, _locked=False):
    if type(no_deps) is not bool:
        raise ManagementError("InvalidArgument", "no_deps must be a boolean; install.py can still execute")
    @asynccontextmanager
    async def lock():
        if _locked:
            yield
        else:
            async with supervisor.maintenance_lock:
                yield

    async with lock():
        supervisor.assert_stopped()
        metadata = await python_metadata(supervisor.config.runtime_python, inventory=True)
        env = tool_env(supervisor.config, metadata)
        steps = []
        result = {"items": [], "steps": steps, "restart_required": True, "partial_changes": False}
        context.partial(result)
        temporary = None
        target = None
        before = None
        try:
            if action == "install_git":
                repository = repository_url(repository)
                selected_name = package_id(name or Path(urlsplit(repository).path).name.removesuffix(".git"))
                roots = await custom_node_roots(supervisor)
                if not roots:
                    raise ManagementError("RootsUnavailable", "Native custom_nodes roots are unavailable")
                for root in roots:
                    if (root / selected_name).exists() or (root / (selected_name + ".disabled")).exists() or (root / ".disabled" / selected_name).exists():
                        raise ManagementError("FileExists", "A plugin with this destination name already exists")
                roots[0].mkdir(parents=True, exist_ok=True)
                target = roots[0] / selected_name
                resolved = await _resolved(supervisor, context, repository, commit,
                    supervisor.config.workspace, steps, env, timeout)
                context.progress(phase="clone", commit=resolved, package=selected_name)
                temporary = Path(tempfile.mkdtemp(prefix=".comani-plugin-", dir=roots[0].parent))
                checkout = temporary / "checkout"
                await _step(supervisor, context, ["git", "clone", "--no-checkout", "--", repository, str(checkout)],
                            supervisor.config.workspace, steps, env, timeout)
                await _step(supervisor, context, ["git", "checkout", "--detach", resolved], checkout, steps, env, timeout)
                from .models import ModelService
                ModelService._rename_directory(checkout, target)
                result["partial_changes"] = True
            elif action == "update_git":
                package_id(package)
                candidates = [item for item in await listing(supervisor) if item["id"] == package or item["name"] == package]
                if len(candidates) != 1 or candidates[0]["source"] != "git":
                    raise ManagementError("AmbiguousPlugin", "Select one installed Git plugin")
                target = Path(candidates[0]["path"])
                before = await git_fact(target)
                if before["dirty"]:
                    raise ManagementError("DirtyWorkingTree", "Plugin has uncommitted changes")
                # 'origin' is resolved by git using its configured external auth.
                resolved = await _resolved(supervisor, context, "origin", commit, target, steps, env, timeout)
                context.progress(phase="checkout", commit=resolved, package=package)
                await _step(supervisor, context, ["git", "fetch", "origin", resolved], target, steps, env, timeout)
                await _step(supervisor, context, ["git", "checkout", "--detach", resolved], target, steps, env, timeout)
                result["partial_changes"] = before["commit"] != resolved
            else:
                raise ManagementError("InvalidArgument", "Unknown Git plugin operation")
            context.progress(phase="dependencies", commit=resolved)
            await _step(supervisor, context, ["git", "submodule", "update", "--init", "--recursive"], target, steps, env, timeout)
            requirement = target / "requirements.txt"
            if not no_deps and requirement.is_file():
                await _step(supervisor, context, [supervisor.config.runtime_python, "-m", "pip", "install", "-r", str(requirement)],
                            target, steps, env, timeout)
            installer = target / "install.py"
            if installer.is_file():
                await _step(supervisor, context, [supervisor.config.runtime_python, str(installer)], target, steps, env, timeout)
            check = await _step(supervisor, context, [supervisor.config.runtime_python, "-m", "pip", "check"],
                                target, steps, env, timeout)
            after = await git_fact(target)
            installed = next((item for item in await listing(supervisor) if item["path"] == str(target)), None)
            item = {"package": package or selected_name, "status": "applied", "before": before,
                    "after": installed or after, "resolved_commit": resolved,
                    "dependencies": "checked" if not check["exit_code"] else "failed",
                    "install_script_may_have_run": installer.is_file()}
            result["items"].append(item)
            result["dependency_changes"] = dependency_changes(metadata,
                await python_metadata(supervisor.config.runtime_python, inventory=True))
            if after["commit"] != resolved or installed is None:
                raise ManagementError("PluginChangeFailed", "Git/plugin postconditions did not match the selected commit")
            return result
        except BaseException:
            if target is not None and target.exists():
                after = await git_fact(target)
                result["partial_changes"] = result["partial_changes"] or before != after
                if not result["items"]:
                    result["items"].append({"package": package or name or target.name, "status": "partial",
                        "before": before, "after": after, "dependencies": "unknown"})
                result["dependency_changes"] = dependency_changes(metadata,
                    await python_metadata(supervisor.config.runtime_python, inventory=True))
            context.partial(result)
            raise
        finally:
            if temporary is not None:
                await _io(shutil.rmtree, temporary)
