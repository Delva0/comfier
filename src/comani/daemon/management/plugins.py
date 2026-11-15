"""Manager CLI adapters with independent, per-package disk verification."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .operations import ManagementError
from .processes import run_process
from .tools import (
    MANAGER_VERSION,
    dependency_changes,
    git_fact,
    manager_argv,
    python_metadata,
    tool_env,
)


def package_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
            or value.lower() in {"all", "comfy", "comfyui"} or value.startswith("-")):
        raise ManagementError("InvalidArgument", "Select explicit registry package IDs; bulk/core aliases are not plugins")
    return value


async def backend(supervisor):
    metadata = await python_metadata(supervisor.config.runtime_python)
    packaged = metadata["packages"].get("comfyui-manager")
    legacy = sorted(path for root in await custom_node_roots(supervisor)
                    for path in root.glob("*/cm-cli.py"))
    if legacy:
        if len(legacy) != 1:
            raise ManagementError("AmbiguousManagerBackend", "Multiple legacy Manager CLIs were found")
        return {"kind": "legacy", "path": str(legacy[0]), "version": None,
                "source": await git_fact(legacy[0].parent),
                "packaged_available": packaged,
                "selection": "existing-legacy-layout",
                "python": metadata["executable"], "capabilities": ["list", "versions", "install", "update", "enable", "disable", "remove"]}
    return {"kind": "packaged" if packaged else "missing",
            "version": packaged["version"] if packaged else None,
            "python": metadata["executable"], "required_version": MANAGER_VERSION,
            "capabilities": ["list", "versions", "install", "update", "enable", "disable", "remove"] if packaged else ["list", "versions"]}


async def custom_node_roots(supervisor):
    store = await supervisor.get_file_store()
    paths = store.folder_paths.get_folder_paths("custom_nodes")
    return tuple(dict.fromkeys(Path(path).expanduser().resolve() for path in paths))


async def listing(supervisor):
    roots = await custom_node_roots(supervisor)
    result = []
    for root in roots:
        if not root.is_dir():
            continue
        candidates = [(path, not path.name.endswith(".disabled")) for path in root.iterdir()
                      if path.is_dir() and not path.name.startswith(".") and path.name != "__pycache__"]
        disabled = root / ".disabled"
        if disabled.is_dir():
            candidates += [(path, False) for path in disabled.iterdir()
                           if path.is_dir() and not path.name.startswith(".")]
        for path, enabled in candidates:
            project = {}
            try:
                with (path / "pyproject.toml").open("rb") as stream:
                    project = tomllib.load(stream).get("project", {})
            except (OSError, ValueError):
                pass
            identifier = project.get("name")
            marker = path / ".git" / ".cnr-id"
            if marker.is_file():
                identifier = marker.read_text().strip()
            facts = await git_fact(path)
            result.append({"id": identifier or path.name.split("@")[0].removesuffix(".disabled"),
                           "name": path.name, "path": str(path), "enabled": enabled,
                           "version": project.get("version"), **facts,
                           "source": "git" if facts["commit"] else "registry" if project else "directory",
                           "live_imported": None})
    return result


async def versions(package):
    package_id(package)
    url = f"https://api.comfy.org/nodes/{quote(package)}/versions?statuses=NodeVersionStatusActive&statuses=NodeVersionStatusPending"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise ManagementError("ProviderError", f"Registry returned HTTP {response.status}")
            return {"package": package, "versions": await response.json()}


async def resolve_registry_version(package, version):
    url = f"https://api.comfy.org/nodes/{quote(package)}/install"
    if version and version != "latest":
        url += "?version=" + quote(version)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    raise ManagementError("RegistryVersionUnavailable", f"Registry returned HTTP {response.status}")
                value = await response.json()
        except (aiohttp.ClientError, TimeoutError):
            raise ManagementError("ProviderError", "Could not resolve the requested registry version") from None
    selected = value.get("version")
    if not isinstance(selected, str) or not re.fullmatch(r"[\w.+-]+", selected):
        raise ManagementError("RegistryVersionUnavailable", "Registry returned no concrete package version")
    return selected


async def registry_repository(package):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            async with session.get(f"https://api.comfy.org/nodes/{quote(package)}") as response:
                if response.status != 200:
                    raise ManagementError("RegistryPackageUnavailable", f"Registry returned HTTP {response.status}")
                value = await response.json()
        except (aiohttp.ClientError, TimeoutError):
            raise ManagementError("ProviderError", "Could not resolve the registry's Git repository") from None
    repository = value.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ManagementError("RegistryPackageUnavailable", "Registry did not provide a Git repository")
    return repository


async def change_disk_state(supervisor, package, action, before, version):
    """Manager's CNR catalog may omit an installed release. Disk state is native.

    Use its active/.disabled layout directly for enable/disable, with create-only
    moves and a subsequent independent scan. No package code/dependency runs.
    """
    desired = action == "enable"
    selected = [item for item in before if version is None or item["version"] == version]
    if not selected:
        raise ManagementError("PluginNotFound", "Selected installed package/version was not found")
    already = any(item["enabled"] for item in selected) if desired else all(not item["enabled"] for item in selected)
    if already:
        return {"package": package, "status": "applied", "before": before, "after": before,
                "dependencies": "unchanged", "dependency_changes": {}, "command": None,
                "install_script_may_have_run": False}
    candidates = [item for item in before if item["enabled"] != desired
                  and (version is None or item["version"] == version)]
    if len(candidates) != 1:
        raise ManagementError("AmbiguousPlugin", "Select one installed package/version for enable or disable")
    item = candidates[0]
    source = Path(item["path"])
    if desired:
        if source.parent.name == ".disabled":
            target = source.parent.parent / source.name.split("@", 1)[0]
        elif source.name.endswith(".disabled"):
            target = source.with_name(source.name.removesuffix(".disabled"))
        else:
            raise ManagementError("PluginLayoutUnknown", "Disabled plugin does not use a recognized Manager layout")
    else:
        destination = source.parent / ".disabled"
        if destination.is_symlink():
            raise ManagementError("PluginLayoutUnknown", "Disabled package directory must not be a symlink")
        destination.mkdir(exist_ok=True)
        suffix = "@" + item["version"].replace(".", "_") if item["source"] == "registry" and item["version"] else ""
        target = destination / (source.name + suffix)
    from .models import ModelService
    ModelService._rename_directory(source, target)
    after = [value for value in await listing(supervisor) if value["id"].lower() == package.lower()]
    okay = any(value["enabled"] == desired and value["path"] == str(target) for value in after)
    result = {"package": package, "status": "applied" if okay else "partial", "before": before, "after": after,
              "dependencies": "unchanged", "dependency_changes": {}, "command": None,
              "install_script_may_have_run": False}
    if not okay:
        raise ManagementError("PluginChangeFailed", "Package directory moved but postconditions failed", result)
    return result


async def change(supervisor, context, action, *, packages, version=None, no_deps=False,
                 manager_version=MANAGER_VERSION, timeout=None):
    packages = [packages] if isinstance(packages, str) else list(packages)
    if not packages or len(packages) > 100:
        raise ManagementError("InvalidArgument", "Select between 1 and 100 packages")
    for package in packages:
        package_id(package)
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[\w.+-]+", version)):
        raise ManagementError("InvalidArgument", "Invalid registry version")
    action = "uninstall" if action in {"remove", "uninstall"} else action
    if action not in {"install", "update", "enable", "disable", "uninstall"}:
        raise ManagementError("InvalidArgument", "Unsupported plugin change")
    if no_deps and action not in {"install", "update"}:
        raise ManagementError("InvalidArgument", "no_deps is supported by install/update only")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        installed = await listing(supervisor)
        git_only = action in {"enable", "disable"} or version == "nightly" or (action == "update" and version is None and all(
            any(item["id"].lower() == package.lower() and item["source"] == "git" for item in installed)
            for package in packages))
        selected = {"kind": "git", "version": None} if git_only else await backend(supervisor)
        if selected["kind"] == "missing":
            raise ManagementError("MissingManagementTool", "Install Manager explicitly in runtime_python")
        if selected["kind"] == "packaged" and selected["version"] != manager_version:
            raise ManagementError("UnsupportedManagementTool", "Manager version differs from the explicit selection",
                                  {"installed": selected["version"], "selected": manager_version})
        metadata = await python_metadata(supervisor.config.runtime_python, inventory=True)
        env = tool_env(supervisor.config, metadata)
        core_before = await git_fact(supervisor.config.comfy_root)
        results = []
        context.partial({"items": results, "restart_required": True, "partial_changes": False})
        for package in packages:
            context.progress(phase=action, package=package)
            before = [item for item in await listing(supervisor) if item["id"].lower() == package.lower()]
            installed_roots = {str(Path(item["path"]).parent.parent if Path(item["path"]).parent.name == ".disabled"
                                   else Path(item["path"]).parent) for item in before}
            if len(installed_roots) > 1:
                raise ManagementError("AmbiguousPlugin", "Package exists in multiple configured custom-node roots")
            if action in {"enable", "disable"}:
                item = await change_disk_state(supervisor, package, action, before, version)
                results.append(item)
                context.partial({"items": results, "restart_required": True,
                                 "partial_changes": any(value["before"] != value["after"] for value in results)})
                continue
            if action in {"install", "update"} and (version == "nightly" or (action == "update" and version is None and before and before[0]["source"] == "git")):
                from .git_plugins import change as git_change
                if before:
                    if len(before) != 1 or before[0]["source"] != "git":
                        raise ManagementError("PluginSourceConflict", "A registry release occupies this name; remove it explicitly or install_git under a new name")
                    git_result = await git_change(supervisor, context, "update_git", package=package,
                        no_deps=no_deps, timeout=timeout, _locked=True)
                else:
                    repository = await registry_repository(package)
                    git_result = await git_change(supervisor, context, "install_git", repository=repository,
                        name=package, no_deps=no_deps, timeout=timeout, _locked=True)
                results.extend(git_result["items"])
                context.partial({"items": results, "restart_required": True,
                                 "partial_changes": any(item["before"] != item["after"] for item in results)})
                continue
            selected_version = None
            actual_action = action
            if action in {"install", "update"}:
                selected_version = await resolve_registry_version(package, version)
                # Manager install(id@version) performs a version switch. Resolve
                # once before executing rather than letting each retry pick latest.
                actual_action = "install"
            spec = package + ("@" + selected_version if selected_version is not None else "")
            args = [actual_action, spec]
            if actual_action == "install":
                args += ["--exit-on-fail"]
                if no_deps:
                    args += ["--no-deps"]
            command = await run_process(context, manager_argv(supervisor.config, selected, args), cwd=supervisor.config.workspace,
                                        env=env, timeout=timeout)
            environment_after = await python_metadata(supervisor.config.runtime_python, inventory=True)
            after = [item for item in await listing(supervisor) if item["id"].lower() == package.lower()]
            observed = bool(after)
            if action == "uninstall":
                observed = not after
            elif action in {"enable", "disable"}:
                observed = bool(after) and any(item["enabled"] == (action == "enable") for item in after)
            elif selected_version is not None:
                observed = any(item["version"] == selected_version and item["enabled"] for item in after)
            # An exit code is only one fact: Manager can swallow per-package errors.
            failure_text = bool(re.search(r"(?im)^.*(?:\[ERROR\]|ERROR:|\[FAILED\]|Failed to |Traceback \(most recent call last\))", command["stdout"] + command["stderr"]))
            dependency = "unknown"
            health = None
            if action in {"install", "update"} and not no_deps and command["exit_code"] == 0:
                health = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "check"],
                                           cwd=supervisor.config.comfy_root, env=env, timeout=60)
                dependency = "checked" if health["exit_code"] == 0 else "failed"
            okay = command["exit_code"] == 0 and observed and not failure_text and dependency != "failed"
            results.append({"package": package, "status": "applied" if okay else "partial" if before != after else "failed",
                            "before": before, "after": after, "command": command,
                            "dependencies": dependency, "dependency_check": health,
                            "resolved_version": selected_version,
                            "dependency_changes": dependency_changes(metadata, environment_after),
                            "install_script_may_have_run": action in {"install", "update"}})
            metadata = environment_after
            context.partial({"items": results, "restart_required": True,
                             "partial_changes": any(item["before"] != item["after"] or item.get("dependency_changes") for item in results)})
        core_after = await git_fact(supervisor.config.comfy_root)
        result = {"items": results, "restart_required": True,
                  "partial_changes": any(item["before"] != item["after"] or item.get("dependency_changes") for item in results),
                  "core_before": core_before, "core_after": core_after}
        context.partial(result)
        if core_before != core_after or any(item["status"] != "applied" for item in results):
            raise ManagementError("PluginChangeFailed", "One or more package postconditions failed; inspect partial changes", result)
        return result
