"""Persistent model leases: stable destinations, inactivity expiry and cleanup."""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from comani.daemon.management import model_resume
from comani.daemon.management.model_resume import ModelResumeCache, RETENTION_SECONDS
from comani.daemon.management.operations import ManagementError


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Model resume leases require flock")
SHA = hashlib.sha256(b"verified model").hexdigest()


def identity(tmp_path, epoch="first"):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    value = root.stat()
    return SimpleNamespace(root=SimpleNamespace(path=root, id=epoch),
                           identity=(value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode))


def test_cache_key_survives_root_epoch_but_separates_digest_path_and_actual_root(tmp_path):
    cache = ModelResumeCache(tmp_path)
    entry = cache.acquire(identity(tmp_path), "folder/model.bin", SHA, "aria2")
    directory = entry.directory
    entry.target.write_bytes(b"partial")
    entry.close()
    resumed = cache.acquire(identity(tmp_path, epoch="new-daemon"), "folder/model.bin", SHA, "aria2")
    assert resumed.directory == directory and resumed.target.read_bytes() == b"partial"
    resumed.close()
    for sha, path, change in [("1" * 64, "folder/model.bin", False),
                              (SHA, "other.bin", False), (SHA, "folder/model.bin", True)]:
        selected = identity(tmp_path)
        if change:
            selected.root.path.chmod(0o700)
            selected = identity(tmp_path)
        other = cache.acquire(selected, path, sha, "aria2")
        assert other.directory != directory and other.target.stat().st_size == 0
        other.close()


def test_expiry_skips_active_and_uses_end_of_attempt_inactivity(tmp_path, monkeypatch):
    now = [100000.0]
    monkeypatch.setattr(model_resume.time, "time", lambda: now[0])
    cache = ModelResumeCache(tmp_path)
    selected = identity(tmp_path)
    entry = cache.acquire(selected, "model.bin", SHA, "http")
    entry.target.write_bytes(b"partial")
    now[0] += RETENTION_SECONDS * 2  # a long active download must not expire
    assert cache.clear() == {"removed": 0, "skipped_active": 1}
    with pytest.raises(ManagementError) as failure:
        cache.acquire(selected, "model.bin", SHA, "http")
    assert failure.value.code == "DestinationBusy"
    entry.close()
    assert json.loads((entry.directory / "manifest.json").read_text())["updated_at"] == now[0]
    now[0] += RETENTION_SECONDS - 1
    resumed = cache.acquire(selected, "model.bin", SHA, "http")
    assert resumed.target.read_bytes() == b"partial"
    resumed.close()
    now[0] += RETENTION_SECONDS + 1
    expired = cache.acquire(selected, "model.bin", SHA, "http")
    assert expired.target.read_bytes() == b""
    expired.close()


def test_clear_skips_inherited_child_lock_and_never_touches_published_models(tmp_path):
    cache = ModelResumeCache(tmp_path)
    selected = identity(tmp_path)
    published = selected.root.path / "complete.bin"
    published.write_bytes(b"complete")
    entry = cache.acquire(selected, "partial.bin", SHA, "aria2")
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], pass_fds=(entry.fd,))
    try:
        entry.close()  # close must not explicitly unlock the inherited flock
        assert cache.clear() == {"removed": 0, "skipped_active": 1}
    finally:
        child.kill()
        child.wait()
    assert cache.clear() == {"removed": 1, "skipped_active": 0}
    assert published.read_bytes() == b"complete"


def test_changing_transport_discards_sparse_piece_state_and_rejects_cache_symlinks(tmp_path):
    cache = ModelResumeCache(tmp_path)
    selected = identity(tmp_path)
    entry = cache.acquire(selected, "model.bin", SHA, "aria2")
    entry.target.write_bytes(b"sparse pieces")
    (entry.directory / "payload.part.aria2").write_bytes(b"piece map")
    entry.close()
    http = cache.acquire(selected, "model.bin", SHA, "http")
    assert http.target.read_bytes() == b"" and not (http.directory / "payload.part.aria2").exists()
    http.close()
    (http.directory / "payload.part.aria2").symlink_to(tmp_path / "absent")
    with pytest.raises(ValueError, match="owned exclusive regular file"):
        cache.acquire(selected, "model.bin", SHA, "http")


def test_cache_does_not_follow_symlinked_workspace_metadata(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".comani").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        ModelResumeCache(tmp_path).clear()
