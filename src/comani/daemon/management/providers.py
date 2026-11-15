"""Small provider resolvers. Public artifacts never contain download credentials."""

from __future__ import annotations

import fnmatch
import os
import re
import time
import uuid
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import aiohttp

from .operations import ManagementError


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


def checked_url(value):
    try:
        if not isinstance(value, str) or any(char.isspace() or char == "\0" for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ManagementError("InvalidArgument", "Expected an HTTP(S) URL without embedded credentials") from None


def provider_headers(url):
    host = (urlsplit(url).hostname or "").lower()
    if host == "huggingface.co" and os.environ.get("HF_TOKEN"):
        return {"Authorization": "Bearer " + os.environ["HF_TOKEN"]}
    if host in {"civitai.com", "www.civitai.com"} and os.environ.get("CIVITAI_TOKEN"):
        return {"Authorization": "Bearer " + os.environ["CIVITAI_TOKEN"]}
    return {}


class ProviderResolver:
    def __init__(self, *, max_sources=1024, retention=3600):
        self.sources = {}
        self.active_sources = {}
        self.max_sources, self.retention = max_sources, retention

    def remember(self, url):
        now = time.time()
        self.sources = {key: value for key, value in self.sources.items()
                        if value[1] > now or self.active_sources.get(key)}
        if len(self.sources) >= self.max_sources:
            raise ManagementError("SourceCapacity", "Resolved source retention capacity reached")
        identifier = uuid.uuid4().hex
        self.sources[identifier] = checked_url(url), now + self.retention
        return {"url": safe_url(url), "source_id": identifier}

    def bind(self, artifact):
        artifact = dict(artifact)
        source = dict(artifact.get("source") or {})
        size = artifact.get("size")
        if size is not None and (type(size) is not int or size < 0):
            raise ManagementError("InvalidArgument", "Artifact size must be a nonnegative byte count")
        if artifact.get("provider") == "huggingface":
            if artifact.get("revision") not in {None, source.get("revision")}:
                raise ManagementError("ArtifactChanged", "HF artifact revision differs from its source identity")
            artifact["revision"] = source.get("revision")
        if artifact.get("provider") == "url":
            if not source.get("source_id"):
                source = self.remember(source.get("url"))
            else:
                source["url"] = safe_url(source.get("url", ""))
        artifact["source"] = source
        return artifact

    def retain(self, artifact):
        identifier = artifact.get("source", {}).get("source_id")
        if identifier:
            value = self.sources.get(identifier)
            if value is None or (value[1] < time.time() and not self.active_sources.get(identifier)):
                raise ManagementError("SourceExpired", "Resolve this URL again before a new download")
            self.active_sources[identifier] = self.active_sources.get(identifier, 0) + 1

    def release(self, artifact):
        identifier = artifact.get("source", {}).get("source_id")
        if identifier:
            count = self.active_sources.get(identifier, 0) - 1
            if count > 0:
                self.active_sources[identifier] = count
            else:
                self.active_sources.pop(identifier, None)

    async def json(self, url):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(url, headers=provider_headers(url)) as response:
                    if response.status != 200:
                        raise ManagementError("ProviderError", f"Provider returned HTTP {response.status}")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > 8 * 1024 * 1024:
                            raise ManagementError("ProviderError", "Provider metadata exceeds 8 MiB")
                        body.extend(chunk)
                    import json
                    return json.loads(body)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ManagementError("ProviderError", "Provider metadata request failed") from None

    async def resolve(self, source, *, revision=None, file_id=None):
        if isinstance(source, dict):
            if source.get("repo_id"):
                return await self.hf(source["repo_id"], revision or source.get("revision", "main"), source.get("path"))
            if source.get("version_id") or source.get("model_id"):
                return await self.civitai(source.get("model_id"), source.get("version_id"), file_id or source.get("file_id"))
            source = source.get("url")
        url = checked_url(source)
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
        artifact = {"provider": "url", "source": self.remember(url), "filename": filename,
                    "size": None, "sha256": None, "revision": None}
        return {"provider": "url", "source": safe_url(url), "revision": None, "files": [artifact]}

    async def hf(self, repo, revision, filename=None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not isinstance(revision, str) or not revision:
            raise ManagementError("InvalidArgument", "Expected an HF owner/repository and revision")
        value = await self.json(f"https://huggingface.co/api/models/{quote(repo, safe='/')}/revision/{quote(revision, safe='')}?blobs=true")
        commit = value.get("sha")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ManagementError("ProviderError", "HF did not resolve an immutable commit")
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
            raise ManagementError("ArtifactNotFound", "HF revision contains no selected file")
        return {"provider": "huggingface", "source": {"repo_id": repo}, "revision": commit, "files": files}

    async def civitai(self, model_id=None, version_id=None, file_id=None):
        if version_id is not None:
            if not str(version_id).isdigit():
                raise ManagementError("InvalidArgument", "Civitai version ID must be numeric")
            versions = [await self.json(f"https://civitai.com/api/v1/model-versions/{version_id}")]
        elif model_id is not None and str(model_id).isdigit():
            model = await self.json(f"https://civitai.com/api/v1/models/{model_id}")
            versions = model.get("modelVersions", [])
        else:
            raise ManagementError("InvalidArgument", "Select a Civitai model/version ID")
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
            raise ManagementError("ArtifactNotFound", "Civitai contains no selected file")
        return {"provider": "civitai", "source": {"model_id": model_id, "version_id": version_id},
                "revision": str(version_id) if version_id else None, "files": files}

    async def address(self, artifact):
        provider, source = artifact.get("provider"), artifact.get("source") or {}
        if provider == "url":
            identifier = source.get("source_id")
            if identifier:
                value = self.sources.get(identifier)
                if value is None or (value[1] < time.time() and not self.active_sources.get(identifier)):
                    raise ManagementError("SourceExpired", "Resolve this URL again before a new download")
                return value[0]
            return checked_url(source.get("url"))
        if provider == "huggingface":
            repo, commit, path = source.get("repo_id"), source.get("revision"), source.get("path")
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit or ""):
                raise ManagementError("InvalidArgument", "Download requires a resolved HF commit")
            return checked_url(f"https://huggingface.co/{quote(repo, safe='/')}/resolve/{commit}/{quote(path, safe='/')}")
        if provider == "civitai":
            value = await self.json(f"https://civitai.com/api/v1/model-versions/{int(source['version_id'])}")
            selected = next((item for item in value.get("files", []) if str(item.get("id")) == str(source.get("file_id"))), None)
            if selected is None:
                raise ManagementError("ArtifactChanged", "Civitai file no longer exists in the selected version")
            checksum = (selected.get("hashes") or {}).get("SHA256", "").lower() or None
            if artifact.get("sha256") and checksum != artifact["sha256"].lower():
                raise ManagementError("ArtifactChanged", "Civitai file digest changed")
            return checked_url(selected["downloadUrl"])
        raise ManagementError("InvalidArgument", "Unknown model provider")

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
            raise ManagementError("InvalidArgument", "Repository manifests currently require an HF model repository")
        for patterns in (allow, ignore):
            if patterns is not None and (not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns)):
                raise ManagementError("InvalidArgument", "allow/ignore must be lists of repository path patterns")
        files = [item for item in resolved["files"]
                 if (allow is None or any(fnmatch.fnmatchcase(item["source"]["path"], pattern) for pattern in allow))
                 and not any(fnmatch.fnmatchcase(item["source"]["path"], pattern) for pattern in (ignore or []))]
        if not files:
            raise ManagementError("ArtifactNotFound", "Manifest selection contains no files")
        return {**resolved, "files": files, "allow": allow, "ignore": ignore,
                "total_size": sum(item["size"] for item in files) if all(type(item.get("size")) is int for item in files) else None}
