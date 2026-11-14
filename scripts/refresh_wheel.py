"""Build this checkout and refresh the existing Torch-free wheel test environment."""

import hashlib
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    project = Path(__file__).resolve().parents[1]
    local = project / ".local"
    directories = {name: local / name for name in ("dist", "tmp", "pip-cache", "uv-cache")}
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(directories["tmp"]),
               PIP_CACHE_DIR=str(directories["pip-cache"]), UV_CACHE_DIR=str(directories["uv-cache"]),
               PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    for name in ("PYTHONPATH", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
        env.pop(name, None)
    subprocess.run([
        sys.executable, "-B", "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
        "--wheel-dir", str(directories["dist"]), str(project),
    ], cwd=project, env=env, check=True)
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    wheel = directories["dist"] / f"comani-{version}-py3-none-any.whl"
    source = project / "src/comani"
    source_hashes = {path.relative_to(source).as_posix(): sha256(path.read_bytes())
                     for path in sorted(source.rglob("*.py"))}
    with zipfile.ZipFile(wheel) as archive:
        wheel_hashes = {name.removeprefix("comani/"): sha256(archive.read(name))
                        for name in archive.namelist() if name.startswith("comani/") and name.endswith(".py")}
    if source_hashes != wheel_hashes:
        raise RuntimeError("Built wheel Python files differ from the current source tree")
    wheel_python = project / ".venv-wheel/bin/python"
    if not wheel_python.is_file():
        raise RuntimeError("Create the documented .venv-wheel SDK environment before refreshing it")
    subprocess.run([
        sys.executable, "-B", "-m", "pip", "--python", str(wheel_python), "install",
        "--no-index", "--no-deps", "--no-compile", "--force-reinstall", str(wheel),
    ], cwd=local, env=env, check=True)
    inspect = """
import hashlib, importlib.util, json, pathlib
import comani
assert importlib.util.find_spec('torch') is None
package = pathlib.Path(comani.__file__).parent
print(json.dumps({'package': str(package), 'files': {
    path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(package.rglob('*.py'))
}}))
"""
    installed = json.loads(subprocess.check_output(
        [str(wheel_python), "-I", "-B", "-c", inspect], cwd=local, env=env, text=True,
    ))
    if not Path(installed["package"]).is_relative_to(project / ".venv-wheel"):
        raise RuntimeError("Wheel import did not originate from .venv-wheel")
    if installed["files"] != source_hashes:
        raise RuntimeError("Installed wheel files differ from the current source tree")
    sdk_python = project / ".venv-sdk/bin/python"
    sdk_origin = None
    if sdk_python.is_file():
        sdk = json.loads(subprocess.check_output(
            [str(sdk_python), "-I", "-B", "-c", inspect], cwd=local, env=env, text=True,
        ))
        if Path(sdk["package"]) != source or sdk["files"] != source_hashes:
            raise RuntimeError("Editable SDK-only environment does not match the current source tree")
        sdk_origin = sdk["package"]
    manifest = {"wheel": str(wheel.relative_to(project)), "wheel_sha256": sha256(wheel.read_bytes()),
                "installed_package": installed["package"], "sdk_only_package": sdk_origin,
                "python_files": source_hashes}
    (project / "docs/wheel_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
