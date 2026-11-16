"""Daemon download source registration and addressing; provider queries are shared."""

from __future__ import annotations

import re
import time
import uuid
from urllib.parse import quote

from comani._model_providers import ProviderQueries, safe_url
from comani._model_providers import checked_url as _checked_url
from comani._model_providers import provider_headers as provider_headers

from .operations import ManagementError


def checked_url(value):
    return _checked_url(value, error_type=ManagementError)


class ProviderResolver(ProviderQueries):
    def __init__(self, *, max_sources=1024, retention=3600):
        super().__init__(error_type=ManagementError)
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
        artifact.pop("_download_url", None)
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

    def url_source(self, url, filename):
        artifact = {"provider": "url", "source": self.remember(url), "filename": filename,
                    "size": None, "sha256": None, "revision": None}
        return {"provider": "url", "source": safe_url(url), "revision": None, "files": [artifact]}

    async def prepare(self, artifact):
        """Resolve a portable source input here, with daemon credentials/state."""
        if isinstance(artifact, str) or (isinstance(artifact, dict) and not artifact.get("provider")):
            resolved = await self.resolve(artifact)
            if len(resolved["files"]) != 1:
                raise ManagementError("InvalidArgument", "Select one file with a source path/file_id or a resolved artifact")
            artifact = resolved["files"][0]
        if not isinstance(artifact, dict) or "files" in artifact:
            raise ManagementError("InvalidArgument", "Expected a model artifact or one-file source descriptor")
        return self.bind(artifact)

    @staticmethod
    def public(artifact):
        result = {key: value for key, value in artifact.items() if not key.startswith("_")}
        result["source"] = dict(artifact.get("source") or {})
        if result.get("provider") == "url":
            result["source"] = {"url": safe_url(result["source"].get("url", ""))}
        return result

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
