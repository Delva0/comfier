"""Local, stateless provider metadata queries using this process's credentials.

This module deliberately has no Client, daemon, file-store or source-cache imports.
Download authentication is selected independently by the receiving daemon.
"""

from __future__ import annotations

import fnmatch
import os
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import aiohttp

from .errors import ComaniError


class ProviderQueryError(ComaniError):
    def __init__(self, code, message, details=None):
        super().__init__(message, details=details)
        self.code = code


def safe_url(value):
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return value.split("?")[0]
        host = parts.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parts.port:
            host += ":" + str(parts.port)
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<redacted URL>"


def checked_url(value, *, error_type=ProviderQueryError):
    try:
        if not isinstance(value, str) or any(char.isspace() or char == "\0" for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise error_type("InvalidArgument", "Expected an HTTP(S) URL without embedded credentials") from None


def provider_headers(url, *, token=None):
    host = (urlsplit(url).hostname or "").lower()
    if host == "huggingface.co" and (token or os.environ.get("HF_TOKEN")):
        return {"Authorization": "Bearer " + (token or os.environ["HF_TOKEN"])}
    if host in {"civitai.com", "www.civitai.com"} and (token or os.environ.get("CIVITAI_TOKEN")):
        return {"Authorization": "Bearer " + (token or os.environ["CIVITAI_TOKEN"])}
    return {}


class ProviderQueries:
    def __init__(self, *, token=None, error_type=ProviderQueryError):
        self.error_type = error_type
        if token is not None and (not isinstance(token, str) or not token or any(char in token for char in "\r\n\0")):
            raise error_type("InvalidArgument", "Provider token must be a nonempty single-line string")
        self.token = token

    def url_source(self, url, filename):
        # The public DTO hides this original transport input in a private field.
        # It is deliberately neither a credential-bearing metadata URL nor a
        # daemon-owned expiring source_id. Signed URLs still expire upstream.
        artifact = {"provider": "url", "source": {"url": safe_url(url)}, "filename": filename,
                    "size": None, "sha256": None, "revision": None, "_download_url": url}
        return {"provider": "url", "source": safe_url(url), "revision": None, "files": [artifact]}

    async def json(self, url):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(url, headers=provider_headers(url, token=self.token)) as response:
                    if response.status != 200:
                        raise self.error_type("ProviderError", f"Provider returned HTTP {response.status}")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > 8 * 1024 * 1024:
                            raise self.error_type("ProviderError", "Provider metadata exceeds 8 MiB")
                        body.extend(chunk)
                    import json
                    return json.loads(body)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise self.error_type("ProviderError", "Provider metadata request failed") from None

    async def resolve(self, source, *, revision=None, file_id=None):
        if isinstance(source, dict):
            if source.get("repo_id"):
                return await self.hf(source["repo_id"], revision or source.get("revision", "main"), source.get("path"))
            if source.get("version_id") or source.get("model_id"):
                return await self.civitai(source.get("model_id"), source.get("version_id"), file_id or source.get("file_id"))
            source = source.get("url")
        url = checked_url(source, error_type=self.error_type)
        parts = urlsplit(url)
        host, path = parts.hostname.lower(), unquote(parts.path).strip("/").split("/")
        if host == "huggingface.co" and len(path) >= 2:
            repo = "/".join(path[:2])
            branch, filename = revision or "main", None
            if len(path) >= 4 and path[2] in {"resolve", "blob", "tree"}:
                branch = revision or path[3]
                filename = "/".join(path[4:]) or None
            return await self.hf(repo, branch, filename)
        if host in {"civitai.com", "www.civitai.com"}:
            version_id = parse_qs(parts.query).get("modelVersionId", [None])[0]
            if path[:3] == ["api", "download", "models"] and len(path) >= 4:
                version_id = path[3]
            model_id = path[1] if path[0] == "models" and len(path) >= 2 else None
            if model_id or version_id:
                return await self.civitai(model_id, version_id, file_id)
        filename = path[-1] or "download"
        return self.url_source(url, filename)

    async def hf(self, repo, revision, filename=None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not isinstance(revision, str) or not revision:
            raise self.error_type("InvalidArgument", "Expected an HF owner/repository and revision")
        value = await self.json(f"https://huggingface.co/api/models/{quote(repo, safe='/')}/revision/{quote(revision, safe='')}?blobs=true")
        commit = value.get("sha")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise self.error_type("ProviderError", "HF did not resolve an immutable commit")
        files = []
        for item in value.get("siblings", []):
            name = item.get("rfilename")
            if filename and name != filename:
                continue
            if not isinstance(name, str):
                continue
            lfs = item.get("lfs") or {}
            files.append({"provider": "huggingface", "source": {"repo_id": repo, "revision": commit, "path": name},
                          "revision": commit, "filename": name.rsplit("/", 1)[-1],
                          "size": lfs.get("size", item.get("size")),
                          "sha256": lfs.get("sha256") or lfs.get("oid"),
                          "git_sha1": item.get("blobId") if not lfs else None})
        if not files:
            raise self.error_type("ArtifactNotFound", "HF revision contains no selected file")
        return {"provider": "huggingface", "source": {"repo_id": repo}, "revision": commit, "files": files}

    async def civitai(self, model_id=None, version_id=None, file_id=None):
        if version_id is not None:
            if not str(version_id).isdigit():
                raise self.error_type("InvalidArgument", "Civitai version ID must be numeric")
            versions = [await self.json(f"https://civitai.com/api/v1/model-versions/{version_id}")]
        elif model_id is not None and str(model_id).isdigit():
            model = await self.json(f"https://civitai.com/api/v1/models/{model_id}")
            versions = model.get("modelVersions", [])
        else:
            raise self.error_type("InvalidArgument", "Select a Civitai model/version ID")
        files = []
        for version in versions:
            for item in version.get("files", []):
                if file_id is not None and str(item.get("id")) != str(file_id):
                    continue
                files.append({"provider": "civitai", "source": {"model_id": version.get("modelId", model_id),
                              "version_id": version["id"], "file_id": item["id"]},
                              "revision": str(version["id"]), "filename": item["name"],
                              "size": round(item["sizeKB"] * 1024) if item.get("sizeKB") is not None else None,
                              "sha256": (item.get("hashes") or {}).get("SHA256", "").lower() or None,
                              "metadata": item.get("metadata", {})})
        if not files:
            raise self.error_type("ArtifactNotFound", "Civitai contains no selected file")
        return {"provider": "civitai", "source": {"model_id": model_id, "version_id": version_id},
                "revision": str(version_id) if version_id else None, "files": files}

    async def versions(self, source):
        result = await self.resolve(source)
        if result["provider"] == "huggingface":
            repo = result["source"]["repo_id"]
            return {"provider": "huggingface", "repo_id": repo,
                    "refs": await self.json(f"https://huggingface.co/api/models/{quote(repo, safe='/')}/refs")}
        return {"provider": result["provider"], "versions": sorted({str(item["revision"]) for item in result["files"] if item["revision"]})}

    async def manifest(self, source, *, revision=None, allow=None, ignore=None):
        resolved = await self.resolve(source, revision=revision)
        if resolved["provider"] != "huggingface":
            raise self.error_type("InvalidArgument", "Repository manifests currently require an HF model repository")
        for patterns in (allow, ignore):
            if patterns is not None and (not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns)):
                raise self.error_type("InvalidArgument", "allow/ignore must be lists of repository path patterns")
        files = [item for item in resolved["files"]
                 if (allow is None or any(fnmatch.fnmatchcase(item["source"]["path"], pattern) for pattern in allow))
                 and not any(fnmatch.fnmatchcase(item["source"]["path"], pattern) for pattern in (ignore or []))]
        if not files:
            raise self.error_type("ArtifactNotFound", "Manifest selection contains no files")
        return {**resolved, "files": files, "allow": allow, "ignore": ignore,
                "total_size": sum(item["size"] for item in files) if all(type(item.get("size")) is int for item in files) else None}

    async def registry_versions(self, package):
        if (not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", package)
                or package.lower() in {"all", "comfy", "comfyui"} or package.startswith("-")):
            raise self.error_type("InvalidArgument", "Select an explicit registry package ID")
        url = f"https://api.comfy.org/nodes/{quote(package)}/versions?statuses=NodeVersionStatusActive&statuses=NodeVersionStatusPending"
        return {"package": package, "versions": await self.json(url)}
