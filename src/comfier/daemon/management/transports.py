"""Explicit download engines with origin-scoped credentials and bounded streaming."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import web
from yarl import URL

from .download_tools import ensure_aria2
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
    return "aria2"


@asynccontextmanager
async def _scoped_response(session, method, address, credentials, request_headers=None):
    """Follow redirects without ever forwarding credentials to another origin."""
    original, current, response = URL(address).origin(), URL(address), None
    try:
        for _ in range(10):
            headers = {"Accept-Encoding": "identity", **(request_headers or {})}
            if current.origin() == original:
                headers.update(credentials)
            response = await session.request(method, current, headers=headers, allow_redirects=False)
            if response.status not in {301, 302, 303, 307, 308}:
                break
            destination = current.join(URL(response.headers.get("Location", "")))
            if (not response.headers.get("Location") or destination.scheme not in {"http", "https"}
                    or destination.user is not None):
                raise ManagementError("ProviderError", "Provider returned an unsafe redirect")
            response.close()
            current = destination
        else:
            raise ManagementError("ProviderError", "Provider exceeded the redirect limit")
        yield response
    finally:
        if response is not None:
            response.close()


@asynccontextmanager
async def _aria_source(address, credentials, connections, timeout, *, probe_validator):
    if not credentials and not probe_validator:
        yield address, "direct", None
        return
    client_timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=timeout or 60)
    async with aiohttp.ClientSession(timeout=client_timeout, auto_decompress=False,
                                    cookie_jar=aiohttp.DummyCookieJar(),
                                    connector=aiohttp.TCPConnector(limit=connections)) as session:
        async with _scoped_response(session, "HEAD", address, credentials) as probe:
            status, final = probe.status, probe.url
            validator = probe.headers.get("ETag")
            encoding, content_type = probe.headers.get("Content-Encoding", "identity"), probe.headers.get("Content-Type", "")
        if status not in {200, 206}:
            async with _scoped_response(session, "GET", address, credentials, {"Range": "bytes=0-0"}) as probe:
                status, final = probe.status, probe.url
                validator = probe.headers.get("ETag")
                encoding, content_type = probe.headers.get("Content-Encoding", "identity"), probe.headers.get("Content-Type", "")
        if status not in {200, 206}:
            raise ManagementError("ProviderError", f"Download source probe returned HTTP {status}",
                                  {"transport": "aria2", "phase": "source_probe", "status": status})
        if encoding != "identity" or "text/html" in content_type.lower():
            raise ManagementError("ProviderError", "Download source did not return an unencoded model response",
                                  {"transport": "aria2", "phase": "source_probe"})
        if not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', validator or ""):
            validator = None
        if not credentials or final.origin() != URL(address).origin():
            yield str(final), "resolved_anonymous" if credentials else "direct", validator
            return
        # Only persistent Bearer sources need this operation-local fixed upstream.
        # aria2 receives no credential; each upstream request is independently scoped.
        route = "/" + uuid.uuid4().hex

        async def relay(request):
            response = None
            wanted = {name: request.headers[name] for name in ("Range", "If-Range", "If-Match")
                      if name in request.headers}
            try:
                async with _scoped_response(session, request.method, address, credentials, wanted) as source:
                    headers = {name: source.headers[name] for name in (
                        "Content-Type", "Content-Length", "Content-Range", "Content-Encoding",
                        "Accept-Ranges", "ETag", "Last-Modified") if name in source.headers}
                    response = web.StreamResponse(status=source.status, headers=headers)
                    await response.prepare(request)
                    if request.method != "HEAD":
                        async for chunk in source.content.iter_chunked(65536):
                            await response.write(chunk)
                        await response.write_eof()
                    return response
            except (aiohttp.ClientError, ConnectionError, TimeoutError):
                if response is not None and response.prepared:
                    response.force_close()
                    return response
                return web.Response(status=502)

        app = web.Application()
        app.router.add_get(route, relay)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}{route}", "credential_relay", validator
        finally:
            await runner.cleanup()


async def _run_aria2(context, binary, address, target, connections, timeout, *, validator, tries, relay,
                    resume=None):
    # Signed URIs never enter argv or persistent files, even if Python is
    # killed before finally runs. aria2 reads an inherited anonymous file FD.
    stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=target.parent)
    try:
        if "\n" in address or "\r" in address:
            raise ManagementError("ProviderError", "Provider returned an invalid download address")
        stream.write(address + "\n dir=" + str(target.parent) + "\n out=" + target.name + "\n")
        if validator:
            if "\n" in validator or "\r" in validator:
                raise ManagementError("ProviderError", "Provider returned an invalid entity validator")
            stream.write(" header=If-Match: " + validator + "\n")
        stream.flush()
        stream.seek(0)
        command = [binary, "--no-conf=true", "--no-netrc=true",
            "--input-file=/proc/self/fd/" + str(stream.fileno()), "--dir=" + str(target.parent), "--out=" + target.name,
            "--max-connection-per-server=" + str(connections), "--split=" + str(connections),
            "--min-split-size=1M", "--allow-overwrite=true", "--auto-file-renaming=false",
            "--continue=" + ("true" if resume is not None else "false"),
            "--auto-save-interval=1", "--check-certificate=true", "--file-allocation=none",
            "--max-tries=" + str(tries), "--retry-wait=1", "--connect-timeout=30", "--timeout=60",
            "--quiet=false", "--show-console-readout=false", "--summary-interval=0",
            "--download-result=hide", "--console-log-level=error"]
        if relay:
            command.append("--no-proxy=127.0.0.1,localhost")
        descriptors = (stream.fileno(),) + ((resume.fd,) if resume is not None else ())
        return await run_process(QuietContext(context), command, cwd=target.parent,
                                 timeout=timeout, output_limit=4096, pass_fds=descriptors)
    finally:
        stream.close()
        if resume is None:
            target.with_name(target.name + ".aria2").unlink(missing_ok=True)


async def external_download(context, resolver, artifact, target, *, transport, connections, timeout,
                            python=sys.executable, resume=None):
    if transport == "aria2":
        tool = await ensure_aria2(context, resolver, cwd=target.parent, timeout=timeout)
        binary = tool["binary"]
        if type(connections) is not int or not 1 <= connections <= 16:
            raise ManagementError("InvalidArgument", "connections must be between 1 and 16")
        immutable = bool(artifact.get("sha256") or artifact.get("git_sha1"))
        for attempt in range(1 if immutable else 4):
            address = await resolver.address(artifact)
            async with _aria_source(address, provider_headers(address), connections, timeout,
                                    probe_validator=not immutable) as (address, route, validator):
                parallel = connections if immutable or validator else 1
                context.progress(phase="downloading", transport="aria2", source_route=route, connections=parallel,
                                 integrity="digest" if immutable else "etag" if validator else "single_response",
                                 attempt=attempt + 1)
                command = await _run_aria2(context, binary, address, target, parallel, timeout,
                                          validator=validator if not immutable else None,
                                          tries=3 if immutable else 1, relay=route == "credential_relay", resume=resume)
            if not command["exit_code"]:
                return
            if immutable or attempt == 3:
                raise ManagementError("ProviderTransferFailed", "aria2 download failed; no model was published",
                                      {"transport": "aria2", "exit_code": command["exit_code"], "diagnostic": diagnostic(command)})
            # Without a digest, a new process may not splice an old sparse file
            # or a changed representation. Re-probe and restart from byte zero.
            target.write_bytes(b"")
            context.log("aria2 source changed or disconnected; retrying from byte zero\n")
            await asyncio.sleep(min(.25 * 2 ** attempt, 2))
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
