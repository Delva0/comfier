"""One application entry, with daemon helpers independent of the CLI layer."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from comani import __main__ as module_entry
from comani.cli import main

PROJECT = Path(__file__).resolve().parents[2]


def test_module_and_console_metadata_use_one_cli_application():
    assert module_entry.main is main
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["comani"] == "comani.cli:main"
    assert not (PROJECT / "src/comani/_runtime").exists()
    assert not (PROJECT / "src/comani/daemon/__main__.py").exists()


def test_lower_layer_import_does_not_load_cli_argparse_or_torch(tmp_path):
    script = """import json,sys
import comani.daemon
import comani.daemon.supervisor
import comani.daemon._paths
import comani.daemon._worker
assert not any(name == 'comani.cli' or name.startswith('comani.cli.') for name in sys.modules)
assert 'argparse' not in sys.modules
assert 'torch' not in sys.modules
print(json.dumps({'cli':False,'argparse':False,'torch':False}))
"""
    env = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"cli": False, "argparse": False, "torch": False}
    for retired in ("comani._runtime", "comani.daemon"):
        removed = subprocess.run([sys.executable, "-m", retired, "--help"], cwd=tmp_path, env=env,
                                 capture_output=True, text=True)
        assert removed.returncode != 0
    for path in (PROJECT / "src/comani/daemon").rglob("*.py"):
        assert "comani.cli" not in path.read_text(), path
