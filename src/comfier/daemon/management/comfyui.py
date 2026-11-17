"""Explicit ComfyUI disk changes; lifecycle remains owned by the supervisor."""

import re
import sys
from pathlib import Path

from .operations import ManagementError
from .processes import run_process
from .tools import (
    comfy_cli,
    git_fact,
    python_metadata,
    tool_env,
)


async def select_target(supervisor, context, action, version, commit):
    if commit is not None:
        if version is not None or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise ManagementError("InvalidArgument", "Select either a version or a complete ComfyUI commit")
        return None, commit.lower()
    if version is None and action == "install":
        version = "nightly"
    cwd = supervisor.config.workspace
    if version == "latest":
        from .providers import ProviderResolver
        release = await ProviderResolver().json("https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest")
        version = release.get("tag_name")
        if not isinstance(version, str) or not re.fullmatch(r"v?\d[\w.+-]*", version):
            raise ManagementError("ProviderError", "ComfyUI release has no verifiable version tag")
    if version is None:
        branch = await run_process(context, ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                                   cwd=supervisor.config.comfy_root, timeout=20)
        if branch["exit_code"] or "/" not in branch["stdout"].strip():
            raise ManagementError("InvalidArgument", "Detached/untracked ComfyUI updates require an explicit version or commit")
        remote, ref = branch["stdout"].strip().split("/", 1)
        if not re.fullmatch(r"[\w.-]+", remote):
            raise ManagementError("InvalidArgument", "Invalid Git upstream remote")
        argv = ["git", "ls-remote", "--exit-code", remote, "refs/heads/" + ref]
        cwd = supervisor.config.comfy_root
    else:
        if not isinstance(version, str) or not re.fullmatch(r"nightly|v?\d[\w.+-]*", version):
            raise ManagementError("InvalidArgument", "Select nightly, latest, or a concrete ComfyUI version")
        ref = "HEAD" if version == "nightly" else "refs/tags/" + (version if version.startswith("v") else "v" + version)
        argv = ["git", "ls-remote", "--exit-code", "https://github.com/Comfy-Org/ComfyUI.git", ref]
        if version != "nightly":
            argv.append(ref + "^{}")
    resolved = await run_process(context, argv, cwd=cwd, timeout=30)
    lines = resolved["stdout"].splitlines()
    if resolved["exit_code"] or not lines or not all(re.fullmatch(r"[0-9a-f]{40}\s+\S+", line) for line in lines):
        raise ManagementError("ProviderError", "Could not fix the requested ComfyUI Git revision")
    return version, lines[-1].split()[0]


async def info(supervisor):
    result = await supervisor.info()
    result = dict(result)
    result["source"] = await git_fact(supervisor.config.comfy_root)
    try:
        result["environment"] = await python_metadata(supervisor.config.runtime_python)
    except (ManagementError, OSError) as exc:
        result["environment"] = {"available": False, "error": str(exc)}
    return result


async def change(supervisor, context, action, *, version=None, commit=None,
                 skip_torch=True, skip_requirements=False, skip_manager=False,
                 cli_version=None, timeout=None):
    if action not in {"install", "update"}:
        raise ManagementError("InvalidArgument", "Unsupported ComfyUI change")
    if any(type(value) is not bool for value in (skip_torch, skip_requirements, skip_manager)):
        raise ManagementError("InvalidArgument", "ComfyUI installation switches must be booleans")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        before = await git_fact(supervisor.config.comfy_root)
        if before["dirty"]:
            raise ManagementError("DirtyWorkingTree", "ComfyUI has uncommitted changes")
        selected_version, expected_commit = await select_target(supervisor, context, action, version, commit)
        context.progress(phase=action, resolved_commit=expected_commit, resolved_version=selected_version)
        if action == "update" and (commit is not None or skip_requirements):
            environment = await python_metadata(supervisor.config.runtime_python)
            env = tool_env(supervisor.config, environment)
            commands = []
            steps = [["git", "fetch", "origin", expected_commit], ["git", "checkout", "--detach", expected_commit]]
            if not skip_requirements:
                steps.append([supervisor.config.runtime_python, "-m", "pip", "install", "-r", "requirements.txt"])
            for argv in steps:
                command = await run_process(context, argv, cwd=supervisor.config.comfy_root, env=env, timeout=timeout)
                commands.append(command)
                after = await git_fact(supervisor.config.comfy_root)
                result = {"before": before, "after": after, "steps": commands, "resolved_commit": expected_commit,
                          "restart_required": True, "partial_changes": before != after,
                          "dependencies": "skipped" if skip_requirements else "requested"}
                context.partial(result)
                if command["exit_code"]:
                    raise ManagementError("ComfyUIChangeFailed", "An explicit core update step failed", result)
            if after["commit"] != expected_commit:
                raise ManagementError("ComfyUIChangeFailed", "Core HEAD differs from the fixed target", result)
            if hasattr(supervisor, "invalidate_roots"):
                await supervisor.invalidate_roots()
            return result
        args = [action]
        if action == "update":
            args += ["comfy", "--no-stash"]
        if selected_version is not None:
            args += ["--version", selected_version]
        if action == "install" and selected_version is None:
            # Typer's commit callback observes ctx.params before the version's
            # default is populated. Keep the explicit ordering for this API.
            args += ["--version", "nightly", "--commit", expected_commit]
        if action == "install":
            if skip_torch:
                args.append("--skip-torch-or-directml")
            if skip_requirements:
                args.append("--skip-requirement")
            if skip_manager:
                args.append("--skip-manager")
            # Hardware policy is explicit configuration; never infer/install CUDA.
            if supervisor.config.cpu:
                args.append("--cpu")
            if not skip_torch and not supervisor.config.cpu:
                raise ManagementError("InvalidArgument", "Choose hardware through explicit bash or use skip_torch=True")
        command = await comfy_cli(supervisor, context, args, cli_version=cli_version, timeout=timeout)
        after = await git_fact(supervisor.config.comfy_root)
        if hasattr(supervisor, "invalidate_roots"):
            await supervisor.invalidate_roots()
        result = {"before": before, "after": after, "command": command,
                  "restart_required": True, "partial_changes": before != after,
                  "resolved_commit": expected_commit, "resolved_version": selected_version}
        context.partial(result)
        if command["exit_code"] or not Path(supervisor.config.comfy_root, "folder_paths.py").is_file() or not after["commit"]:
            raise ManagementError("ComfyUIChangeFailed", "ComfyUI change did not satisfy its postconditions", result)
        if after["commit"] != expected_commit:
            raise ManagementError("ComfyUIChangeFailed", "Installed HEAD differs from the requested commit", result)
        return result


async def prepare_runtime(supervisor, context, *, package, timeout=None):
    """Install a caller-selected wheel/requirement in the target environment."""
    if not isinstance(package, str) or not package or package.startswith("-") or "\0" in package:
        raise ManagementError("InvalidArgument", "Choose a local wheel path or explicit package requirement")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        before = await python_metadata(supervisor.config.runtime_python)
        env = tool_env(supervisor.config, before)
        package_install = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "install", "--force-reinstall", "--no-deps", package],
                                    cwd=supervisor.config.workspace, env=env, timeout=timeout)
        command = package_install
        if package_install["exit_code"] == 0:
            # Resolve declared dependencies after replacing the exact requested
            # wheel, without force-reinstalling unrelated existing dependencies.
            command = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "install", package],
                                        cwd=supervisor.config.workspace, env=env, timeout=timeout)
        after = await python_metadata(supervisor.config.runtime_python)
        check = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "check"],
                                  cwd=supervisor.config.workspace, env=env, timeout=60)
        result = {"before": before, "after": after, "package_install": package_install,
                  "command": command, "dependency_check": check,
                  "partial_changes": before != after, "restart_required": True}
        context.partial(result)
        if package_install["exit_code"] or command["exit_code"] or check["exit_code"] or not all(name in after["packages"] for name in ("comfier", "aiohttp", "cloudpickle")):
            raise ManagementError("RuntimePreparationFailed", "Target runtime package/dependency postconditions failed", result)
        return result


async def create_environment(supervisor, context, *, path, python=None, timeout=None):
    """Create an explicitly requested isolated venv; never mutate management Python."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise ManagementError("InvalidArgument", "Select an environment directory")
    requested = Path(path).expanduser().absolute()
    target = requested.resolve()
    if target.parent == target or requested.is_symlink():
        raise ManagementError("FileExists", "Environment destination must be a new directory")
    python = python or sys.executable
    if not isinstance(python, str) or not python or python.startswith("-") or "\0" in python:
        raise ManagementError("InvalidArgument", "Choose a base Python executable")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        if target.exists() or target.is_symlink():
            raise ManagementError("FileExists", "Environment destination must be a new directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        executable = target / "bin" / "python"
        result = {"path": str(target), "python": str(executable), "created": False,
                  "isolated": True, "base_python": python}
        context.partial(result)
        command = await run_process(context, [python, "-m", "venv", str(target)],
                                    cwd=supervisor.config.workspace, timeout=timeout)
        result["command"] = command
        if command["exit_code"]:
            raise ManagementError("EnvironmentCreationFailed", "Target venv creation failed; inspect the partial directory", result)
        metadata = await python_metadata(str(executable))
        configuration = (target / "pyvenv.cfg").read_text()
        if Path(metadata["prefix"]).resolve() != target or "include-system-site-packages = false" not in configuration.lower():
            raise ManagementError("EnvironmentCreationFailed", "Target environment isolation/prefix did not match", result)
        result["created"], result["environment"] = True, metadata
        return result


async def prepare_tools(supervisor, context, *, cli_revision=None, cli_version=None,
                        manager_version=None, hub_version=None, skip_cli=False,
                        skip_manager=False, timeout=None):
    """Ensure explicitly requested tools, preserving installed choices by default.

    Missing CLI/Manager packages use their normal release channel. An installed
    core's manager_requirements.txt takes precedence when adding a missing Manager.
    None means reuse-or-install; skipping a tool is an explicit boolean choice.
    """
    if any(type(value) is not bool for value in (skip_cli, skip_manager)):
        raise ManagementError("InvalidArgument", "Tool skip switches must be booleans")
    if cli_revision is not None:
        if (not isinstance(cli_revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", cli_revision)
                or ".." in cli_revision):
            raise ManagementError("InvalidArgument", "Select a comfy-cli Git revision, tag or branch")
        if cli_version is not None or skip_cli:
            raise ManagementError("InvalidArgument", "Select one CLI source or skip_cli")
    if skip_cli and cli_version is not None or skip_manager and manager_version is not None:
        raise ManagementError("InvalidArgument", "A skipped tool cannot have an explicit version")
    for version in (cli_version, manager_version, hub_version):
        if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[0-9][\w.+-]*", version)):
            raise ManagementError("InvalidArgument", "Tool release selections require concrete versions")
    async with supervisor.maintenance_lock:
        supervisor.assert_stopped()
        before = await python_metadata(supervisor.config.runtime_python)
        installed = before["packages"]
        env = tool_env(supervisor.config, before)
        requirements, selected = [], {}
        if not skip_cli:
            selected["comfy-cli"] = "git" if cli_revision else "release" if cli_version else "installed-or-release"
            if cli_revision:
                requirements.append("comfy-cli @ git+https://github.com/Comfy-Org/comfy-cli.git@" + cli_revision)
            elif cli_version:
                requirements.append("comfy-cli==" + cli_version)
            elif "comfy-cli" not in installed:
                requirements.append("comfy-cli")
        if not skip_manager:
            selected["comfyui-manager"] = "explicit" if manager_version else "installed"
            if manager_version:
                requirements.append("comfyui-manager==" + manager_version)
            elif "comfyui-manager" not in installed:
                manager_requirements = Path(supervisor.config.comfy_root) / "manager_requirements.txt"
                if manager_requirements.is_file():
                    requirements += ["-r", str(manager_requirements)]
                    selected["comfyui-manager"] = "core-requirements"
                else:
                    requirements.append("comfyui-manager")
                    selected["comfyui-manager"] = "release"
        if hub_version:
            requirements.append("huggingface-hub==" + hub_version)
            selected["huggingface-hub"] = "explicit"
        if not selected:
            raise ManagementError("InvalidArgument", "Select at least one management/provider tool")
        command = None
        if requirements:
            command = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "install", *requirements],
                                        cwd=supervisor.config.workspace, env=env, timeout=timeout)
        after = await python_metadata(supervisor.config.runtime_python)
        check = await run_process(context, [supervisor.config.runtime_python, "-m", "pip", "check"],
                                  cwd=supervisor.config.workspace, env=env, timeout=60)
        packages = after["packages"]
        matches = all(name in packages for name in selected)
        cli = packages.get("comfy-cli", {})
        if cli_revision:
            vcs = cli.get("direct_url", {}).get("vcs_info", {})
            matches = matches and bool(vcs.get("commit_id"))
            if re.fullmatch(r"[0-9a-fA-F]{40}", cli_revision):
                matches = matches and vcs.get("commit_id", "").lower() == cli_revision.lower()
            else:
                matches = matches and vcs.get("requested_revision") == cli_revision
        for name, version in (("comfy-cli", cli_version), ("comfyui-manager", manager_version),
                              ("huggingface-hub", hub_version)):
            if version:
                matches = matches and packages.get(name, {}).get("version") == version
        result = {"before": before, "after": after, "command": command, "dependency_check": check,
                  "selection": selected, "partial_changes": before != after, "restart_required": True}
        context.partial(result)
        if (command is not None and command["exit_code"]) or check["exit_code"] or not matches:
            raise ManagementError("ToolPreparationFailed", "Tool selection/dependency postconditions failed", result)
        return result
