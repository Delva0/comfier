"""Version-aware subprocess adapters, without importing management CLIs."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from .operations import ManagementError
from .processes import child_env, run_process


async def python_metadata(python, *, inventory=False):
    code = """import importlib.metadata as m,json,sys,os
result={'executable':sys.executable,'prefix':sys.prefix,'packages':{},
        'python_version':sys.version.split()[0],'implementation':sys.implementation.name}
for name in ('comfy-cli','comfyui-manager','comani','aiohttp','cloudpickle','huggingface-hub','hf-xet','torch'):
 try:
  d=m.distribution(name)
  result['packages'][name]={'version':d.version,'direct_url':json.loads(d.read_text('direct_url.json') or '{}')}
 except m.PackageNotFoundError: pass
"""
    if inventory:
        code += "result['distributions']={d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name']}\n"
    code += "print(json.dumps(result))\n"
    proc = await asyncio.create_subprocess_exec(python, "-I", "-c", code, env=child_env(),
                                               stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ManagementError("InvalidRuntimePython", "Configured runtime Python could not report metadata")
    try:
        result = json.loads(stdout)
        from .providers import safe_url
        for package in result.get("packages", {}).values():
            if package.get("direct_url", {}).get("url"):
                package["direct_url"]["url"] = safe_url(package["direct_url"]["url"])
        return result
    except (ValueError, UnicodeError):
        raise ManagementError("InvalidRuntimePython", "Runtime Python returned invalid metadata") from None


def dependency_changes(before, after):
    old, new = before.get("distributions", {}), after.get("distributions", {})
    return {name: {"before": old.get(name), "after": new.get(name)}
            for name in sorted(set(old) | set(new)) if old.get(name) != new.get(name)}


async def git_fact(path):
    if not (Path(path) / ".git").exists():
        return {"commit": None, "dirty": None, "repository": None}
    result = {}
    for key, args in (("commit", ["rev-parse", "HEAD"]),
                      ("dirty", ["status", "--porcelain"]),
                      ("repository", ["config", "--get", "remote.origin.url"])):
        proc = await asyncio.create_subprocess_exec("git", "-C", str(path), *args,
                                                   stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL, env=child_env())
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 15)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        value = stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else None
        result[key] = bool(value) if key == "dirty" else value
    # Repository URLs in .git/config may include credentials.
    if result["repository"]:
        from .providers import safe_url
        result["repository"] = safe_url(result["repository"])
    return result


def tool_env(config, metadata):
    prefix = metadata["prefix"]
    import os
    return {"VIRTUAL_ENV": prefix, "PATH": str(Path(prefix) / "bin") + os.pathsep + os.defpath,
            "COMFYUI_PATH": str(config.comfy_root), "COMFY_CLI_ENABLE_TELEMETRY": "false",
            "CI": "true", "PIP_NO_INPUT": "1"}


def manager_argv(config, backend, arguments):
    # Manager does not parse ComfyUI's base/extra-model-path arguments itself.
    # Preload the same pure path helper used by cold-root discovery, in this
    # target subprocess, before running its unmodified official CLI entrypoint.
    code = """import json,runpy,sys
helper,config,entry,*arguments=sys.argv[1:]
runpy.run_path(helper)['configured_paths'](json.loads(config))
sys.argv=[entry,*arguments]
if entry == 'cm_cli': runpy.run_module(entry,run_name='__main__')
else: runpy.run_path(entry,run_name='__main__')
"""
    helper = Path(__file__).resolve().parents[1] / "_paths.py"
    params = {"comfy_root": config.comfy_root, "comfy_args": list(config.comfy_args)}
    entry = backend["path"] if backend["kind"] == "legacy" else "cm_cli"
    return [config.runtime_python, "-c", code, str(helper), json.dumps(params), entry, *arguments]


async def comfy_cli(supervisor, context, args, *, cli_version=None, timeout=None):
    metadata = await python_metadata(supervisor.config.runtime_python)
    package = metadata["packages"].get("comfy-cli")
    if not package:
        raise ManagementError("MissingManagementTool", "Install comfy-cli in runtime_python with prepare_tools() explicitly")
    actual_commit = package["direct_url"].get("vcs_info", {}).get("commit_id")
    if cli_version is not None and cli_version not in {package["version"], actual_commit}:
        raise ManagementError("UnsupportedManagementTool", "Installed comfy-cli differs from the selected version")
    config = supervisor.config
    env = tool_env(config, metadata)
    import hashlib
    instance = hashlib.sha256((str(config.workspace) + "\0" + str(config.comfy_root)).encode()).hexdigest()[:20]
    home = Path(config.workspace) / ".comani" / "tool-home" / instance
    home.mkdir(parents=True, exist_ok=True)
    # The official CLI derives its ConfigManager path from expanduser('~'), not
    # XDG_CONFIG_HOME. Its mutable recent/default workspace is instance-local.
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["NO_COLOR"] = "1"
    base = [config.runtime_python, "-m", "comfy_cli"]
    await require_flags(context, [*base, "--help"], {"--skip-prompt", "--workspace"},
                        cwd=config.workspace, env=env, tool="comfy-cli", version=package["version"])
    help_args = [args[0], "--help"]
    required = {value.split("=", 1)[0] for value in args if value.startswith("--")}
    await require_flags(context, [*base, *help_args], required,
                        cwd=config.workspace, env=env, tool="comfy-cli", version=package["version"])
    # Prove the selected environment before any install/update executes. The CLI's
    # VIRTUAL_ENV-first resolver must pick exactly the configured interpreter.
    check = await run_process(context, [config.runtime_python, "-c",
        "from comfy_cli.resolve_python import resolve_workspace_python;print(resolve_workspace_python())"],
        cwd=config.workspace, env=env, timeout=20)
    if check["exit_code"]:
        raise ManagementError("UnsupportedManagementTool", "Installed comfy-cli cannot resolve the configured runtime interpreter",
                              {"version": package["version"], "required_capability": "resolve_workspace_python"})
    selected = check["stdout"].strip()
    try:
        selected_environment = await python_metadata(selected)
    except (OSError, ManagementError):
        raise ManagementError("RuntimePythonMismatch", "comfy-cli selected an unusable Python interpreter") from None
    # Different venvs often symlink the same base executable. Comparing realpath
    # alone would incorrectly accept an installation into another environment.
    if Path(selected_environment["prefix"]).resolve() != Path(metadata["prefix"]).resolve():
        raise ManagementError("RuntimePythonMismatch", "comfy-cli selected another Python environment")
    return await run_process(context, [config.runtime_python, "-m", "comfy_cli", "--skip-prompt",
        "--workspace", str(config.comfy_root), *args], cwd=config.workspace, env=env, timeout=timeout)


async def require_flags(context, argv, required, *, cwd, env, tool, version=None):
    """Probe the installed official entrypoint before a mutating command."""
    # Rich/Typer truncates long option names at the default non-TTY width.
    result = await run_process(context, argv, cwd=cwd, env={**env, "COLUMNS": "240", "NO_COLOR": "1"}, timeout=30)
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result["stdout"] + result["stderr"])
    available = set(re.findall(r"--[a-z][a-z0-9-]*", output))
    missing = sorted(required - available)
    if result["exit_code"] or missing:
        raise ManagementError("UnsupportedManagementTool", f"Installed {tool} does not provide the required command interface",
                              {"version": version, "missing_flags": missing, "probe_exit_code": result["exit_code"]})
