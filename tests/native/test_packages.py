import base64
import hashlib
import io
import sys
import uuid
import zipfile

import pytest

from comani.daemon.python_node import Artifacts
from comani.errors import PackageConflict


def package(artifacts, name, *, value=7, entry=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        def write(filename, source):
            archive.writestr(zipfile.ZipInfo(f"{name}/{filename}", (1980, 1, 1, 0, 0, 0)), source)

        write("__init__.py", "counter = 0\n")
        write("relative.py", f"value = {value}\n")
        write("late.py", "value = 3\n")
        write("pipeline.py", f"import {name}\nfrom {name}.relative import value as absolute\nfrom .relative import value as relative\nasync def main():\n import asyncio\n await asyncio.sleep(0)\n from {name}.late import value as late\n {name}.counter += 1\n return absolute, relative, late, {name}.counter\n")
    raw = output.getvalue()
    artifact = hashlib.sha256(raw).hexdigest()
    artifacts.put(artifact, base64.b64encode(raw).decode(), "package")
    return {"kind": "package", "artifact_id": artifact, "package_root": name, "entrypoint": entry or name + ".pipeline:main"}


async def test_packages_use_normal_absolute_and_relative_imports_across_sessions():
    name = "comani_test_" + uuid.uuid4().hex
    first = Artifacts()
    second = Artifacts()
    try:
        first.session = "first"
        code = package(first, name, entry="pipeline:main")
        function = first.load(code)
        assert await function() == (7, 7, 3, 1)
        module = sys.modules[name]
        first.clear_session("first")
        first.directory.cleanup()
        assert sys.modules[name] is module
        second.session = "second"
        same = package(second, name)
        assert same["artifact_id"] == code["artifact_id"]
        assert second.load(same) is function
        assert await second.load(same)() == (7, 7, 3, 2)
        other = package(second, name, value=8)
        with pytest.raises(PackageConflict, match="different content hash"):
            second.load(other)
        assert sys.modules[name] is module
    finally:
        first.directory.cleanup()
        second.directory.cleanup()


def test_packages_cannot_shadow_installed_modules():
    artifacts = Artifacts()
    try:
        code = package(artifacts, "json")
        with pytest.raises(PackageConflict, match="runtime environment"):
            artifacts.load(code)
    finally:
        artifacts.directory.cleanup()
