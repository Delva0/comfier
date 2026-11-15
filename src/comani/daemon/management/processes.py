"""Foreground subprocesses with bounded output and owned process-group cleanup."""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import signal
import time

from .operations import ManagementError


def child_env(overrides=None):
    # Management credentials do not become shell credentials by inheritance.
    env = {key: value for key, value in os.environ.items()
           if not re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|AUTHORIZATION|COOKIE", key, re.I)
           and key not in {"VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH"}}
    for key, value in (overrides or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value or "=" in key:
            raise ManagementError("InvalidArgument", "Environment overrides must be string pairs")
        env[key] = value
    return env


async def terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # The shell may have exited while a descendant still owns its pipes. Wait
    # for the owned group, rather than treating the leader's exit as cleanup.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def run_process(context, argv, *, cwd, env=None, timeout=None, output_limit=1024 * 1024):
    if os.name != "posix":
        raise ManagementError("UnsupportedPlatform", "Management subprocesses require Linux")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        raise ManagementError("InvalidArgument", "Timeout must be positive")
    started = time.monotonic()
    creation = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=child_env(env), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True))
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await creation
        await terminate(process)
        await process.communicate()
        raise
    output, truncated = {}, {}

    async def read(name, stream):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        parts, size = [], 0
        while data := await stream.read(16384):
            text = decoder.decode(data)
            context.log(text, name)
            available = min(max(0, output_limit - size), context.record.capture_remaining)
            keep = data[:available]
            context.record.capture_remaining -= len(keep)
            if keep:
                parts.append(keep)
            size += len(data)
        tail = decoder.decode(b"", final=True)
        if tail:
            context.log(tail, name)
        output[name] = b"".join(parts).decode("utf-8", "replace")
        truncated[name] = size > sum(len(part) for part in parts)

    readers = [asyncio.create_task(read("stdout", process.stdout)),
               asyncio.create_task(read("stderr", process.stderr))]
    completion = asyncio.gather(process.wait(), *readers)
    timed_out = False
    try:
        if timeout is None:
            await asyncio.shield(completion)
        else:
            try:
                await asyncio.wait_for(asyncio.shield(completion), timeout)
            except TimeoutError:
                timed_out = True
                await terminate(process)
                await completion
    except asyncio.CancelledError:
        await terminate(process)
        await completion
        context.partial({"exit_code": process.returncode, **output,
                         "truncated": truncated, "partial_changes": True})
        raise
    finally:
        await asyncio.gather(*readers)
    result = {"exit_code": process.returncode, **output, "truncated": truncated,
              "duration": time.monotonic() - started, "operation_id": context.record.id}
    if timed_out:
        context.partial(result)
        raise ManagementError("OperationTimeout", "Command timeout; completed changes are not rolled back", result)
    return result
