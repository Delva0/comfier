"""Independently compare current source, wheel archive, and installed SDK bytes."""
import hashlib
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path


def hashes(root):
    return {file.relative_to(root).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
            for file in root.rglob("*.py")}


project = Path(__file__).resolve().parents[2]
wheel = project / ".local/dist/comani-0.2.0-py3-none-any.whl"
source = hashes(project / "src/comani")
with zipfile.ZipFile(wheel) as archive:
    packaged = {name.removeprefix("comani/"): hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist() if name.startswith("comani/") and name.endswith(".py")}
with tempfile.TemporaryDirectory() as temporary:
    checked = subprocess.run([str(project / ".venv-wheel/bin/python"), "-B", "-c",
                              "import comani,importlib.util; assert importlib.util.find_spec('torch') is None; print(comani.__file__)"],
                             cwd=temporary, text=True, capture_output=True, check=True)
installed_root = Path(checked.stdout.strip()).parent
assert installed_root.is_relative_to(project / ".venv-wheel")
installed = hashes(installed_root)
assert source == packaged == installed
report = {"passed": True, "python_file_count": len(source),
          "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
          "installed_origin": str(installed_root), "torch_absent": True, "python_files": source}
(project / "tests/stress/wheel_audit_results.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
