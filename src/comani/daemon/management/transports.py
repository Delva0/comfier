"""Optional existing download engines; no implicit package/system installation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile

from .operations import ManagementError
from .processes import run_process
from .providers import provider_headers, safe_url


class QuietContext:
    """Download-engine diagnostics may contain expiring/signed resource URLs."""
    def __init__(self, context):
        self.record = context.record

    def log(self, *args, **kwargs):
        pass

    def partial(self, value):
        pass


def diagnostic(command):
    value = (command.get("stderr", "") + "\n" + command.get("stdout", ""))[-4000:]
    value = re.sub(r"https?://[^\s\"'<>]+", lambda match: safe_url(match[0]), value)
    for name in ("HF_TOKEN", "CIVITAI_TOKEN", "CIVITAI_API_TOKEN"):
        if os.environ.get(name):
            value = value.replace(os.environ[name], "<redacted>")
    return value.strip()


def select_transport(artifact, transport, *, hf_available=False):
    if transport not in {"auto", "http", "aria2", "hf"}:
        raise ManagementError("InvalidArgument", "transport must be auto, http, aria2, or hf")
    if transport != "auto":
        return transport
    if artifact.get("provider") == "huggingface" and hf_available:
        return "hf"
    if artifact.get("sha256") and shutil.which("aria2c"):
        return "aria2"
    return "http"


async def external_download(context, resolver, artifact, target, *, transport, connections, timeout,
                            python=sys.executable):
    if transport == "aria2":
        binary = shutil.which("aria2c")
        if not binary:
            raise ManagementError("MissingDownloadTool", "aria2c is not installed; install it explicitly or select http")
        if type(connections) is not int or not 1 <= connections <= 16:
            raise ManagementError("InvalidArgument", "connections must be between 1 and 16")
        if not artifact.get("sha256"):
            raise ManagementError("UnverifiableTransport", "aria2 requires an expected SHA256; select http for validator-based acquisition")
        address = await resolver.address(artifact)
        headers = provider_headers(address)
        if headers:
            # aria2 forwards custom Authorization headers across redirects.
            raise ManagementError("UnsafeDownloadTransport",
                                  "aria2 cannot scope provider credentials across redirects; select http")
        # Signed URIs stay in an owner-only input file, never in argv.
        fd, name = tempfile.mkstemp(prefix="aria-input-", dir=target.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(address + "\n")
                # aria2's global --out is ignored for --input-file entries.
                stream.write(" dir=" + str(target.parent) + "\n")
                stream.write(" out=" + target.name + "\n")
            command = await run_process(QuietContext(context), [binary,
                "--input-file=" + name, "--dir=" + str(target.parent), "--out=" + target.name,
                "--max-connection-per-server=" + str(connections), "--split=" + str(connections),
                "--min-split-size=1M", "--allow-overwrite=true", "--auto-file-renaming=false",
                "--continue=false", "--check-certificate=true", "--file-allocation=none",
                "--max-tries=3", "--retry-wait=1", "--connect-timeout=30", "--timeout=60",
                "--quiet=false", "--show-console-readout=false", "--summary-interval=0",
                "--download-result=hide", "--console-log-level=error"],
                cwd=target.parent, timeout=timeout, output_limit=4096)
        finally:
            os.unlink(name)
            target.with_name(target.name + ".aria2").unlink(missing_ok=True)
        if command["exit_code"]:
            raise ManagementError("ProviderTransferFailed", "aria2 download failed; no model was published",
                                  {"transport": "aria2", "exit_code": command["exit_code"], "diagnostic": diagnostic(command)})
        return
    if artifact.get("provider") != "huggingface":
        raise ManagementError("InvalidArgument", "hf transport requires an HF artifact")
    # The official Hub client selects its installed HTTP/Xet implementation and
    # fixed-revision cache. It runs in a subprocess, isolated from daemon globals.
    source = artifact["source"]
    cache = tempfile.mkdtemp(prefix=target.name + "-cache-", dir=target.parent)
    request = {"repo_id": source["repo_id"], "filename": source["path"],
               "revision": source["revision"], "target": str(target),
               "cache_dir": cache}
    code = """import json,os,shutil,sys
from huggingface_hub import hf_hub_download
p=json.loads(sys.argv[1]);target=p.pop('target')
path=hf_hub_download(**p,token=os.environ.get('HF_TOKEN'))
shutil.copyfile(path,target)
"""
    env = {"HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    if os.environ.get("HF_TOKEN"):
        env["HF_TOKEN"] = os.environ["HF_TOKEN"]
    try:
        command = await run_process(QuietContext(context), [python, "-c", code, json.dumps(request)],
                                    cwd=target.parent, env=env, timeout=timeout, output_limit=4096)
    finally:
        await asyncio.to_thread(shutil.rmtree, cache, True)
    if command["exit_code"]:
        raise ManagementError("ProviderTransferFailed", "Official HF transfer failed; verify repository access and installed Hub/Xet support",
                              {"transport": "hf", "exit_code": command["exit_code"], "diagnostic": diagnostic(command)})
