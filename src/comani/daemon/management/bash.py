"""A shell namespace is OS I/O and never a Comfy computation."""

from pathlib import Path

from .operations import ManagementError
from .processes import run_process


async def run(supervisor, context, *, script, cwd=None, env=None, timeout=None, check=True):
    if not isinstance(script, str) or not script or "\0" in script:
        raise ManagementError("InvalidArgument", "script must be a nonempty string")
    if type(check) is not bool:
        raise ManagementError("InvalidArgument", "check must be a boolean")
    target = Path(cwd or supervisor.config.workspace).expanduser()
    if not target.is_absolute():
        target = Path(supervisor.config.workspace) / target
    target = target.resolve()
    result = await run_process(context, ["bash", "--noprofile", "--norc", "-c", script],
                               cwd=target, env=env, timeout=timeout)
    if check and result["exit_code"]:
        context.partial(result)
        raise ManagementError("CommandFailed", f"Command exited with {result['exit_code']}", result)
    return result
