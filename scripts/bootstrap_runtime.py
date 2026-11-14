"""Prepare an independent development environment; never modify ComfyUI's venv."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--seed-from", type=Path)
    parser.add_argument("--uv", default=shutil.which("uv"))
    args = parser.parse_args()
    uv = args.uv or str(Path.home() / ".local/bin/uv")
    comfy = args.comfy_root.resolve()
    if not (comfy / "execution.py").is_file():
        parser.error("--comfy-root must name a ComfyUI source checkout")
    environment = project / ".venv"
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    fresh = not python.exists()
    if fresh:
        subprocess.run([uv, "venv", "--python", args.python, str(environment)], check=True)
    if args.seed_from:
        if not fresh:
            parser.error("--seed-from is only allowed when creating a fresh environment")
        source_python = args.seed_from.resolve() / "bin/python"
        probe = "import json,sys,sysconfig; print(json.dumps([sys.version_info[:3],sysconfig.get_paths()['purelib']]))"
        source = json.loads(subprocess.check_output([str(source_python), "-c", probe], text=True))
        target = json.loads(subprocess.check_output([str(python), "-c", probe], text=True))
        if source[0] != target[0]:
            parser.error("seed and target Python versions must match exactly")
        source_site, target_site = Path(source[1]), Path(target[1])
        if list(source_site.glob("__editable__*")):
            parser.error("seed contains editable imports; use a clean seed or omit --seed-from")
        shutil.copytree(source_site, target_site, dirs_exist_ok=True)
    command = [uv, "pip", "install", "--python", str(python)]
    subprocess.run(
        command + ["--constraint", str(project / "runtime-constraints.txt"),
                   "--requirement", str(comfy / "requirements.txt"),
                   "--editable", str(project) + "[test]"], check=True,
    )
    # A copied optional xformers wheel is tied to its original torch build.
    subprocess.run([uv, "pip", "uninstall", "--python", str(python), "xformers"], check=True)
    subprocess.run(command + ["--upgrade", "opencv-python-headless"], check=True)
    subprocess.run([uv, "pip", "check", "--python", str(python)], check=True)
    print(python)


if __name__ == "__main__":
    main()
