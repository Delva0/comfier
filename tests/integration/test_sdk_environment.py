"""The public SDK must remain usable on a laptop without native dependencies."""

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


def test_sdk_import_and_graph_capture_without_torch(tmp_path):
    project = Path(__file__).resolve().parents[2]
    python = project / ".venv-sdk" / "bin" / "python"
    if not python.exists():
        pytest.skip("Create the documented SDK-only .venv-sdk to run this environment gate")
    script = """
import asyncio, importlib.util, json, pathlib, sys
assert importlib.util.find_spec('torch') is None
assert importlib.util.find_spec('execution') is None
from comani import Client
import comani
async def capture():
    remote = Client('http://127.0.0.1:1')
    with remote.graph() as graph:
        value, = await remote.python(lambda: {'items': [1, 2]})
        graph.output('value', value)
    assert len(graph.nodes) == 1
    assert remote._opening is None
    return {'package': comani.__file__, 'node_count': len(graph.nodes)}
print(json.dumps(asyncio.run(capture())))
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-B", "-c", script], cwd=tmp_path, env=env,
        text=True, capture_output=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert Path(result["package"]).is_relative_to(project / "src")
    assert result["node_count"] == 1


def test_built_wheel_import_and_daemon_entrypoint(tmp_path):
    project = Path(__file__).resolve().parents[2]
    python = project / ".venv-wheel/bin/python"
    if not python.exists():
        pytest.skip("Build and install the wheel into the documented .venv-wheel first")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(python), "-B", "-c",
         "import comani,importlib.util; assert importlib.util.find_spec('torch') is None; print(comani.__file__)"],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=True,
    )
    package = Path(completed.stdout.strip()).parent
    assert package.is_relative_to(project / ".venv-wheel")
    source = project / "src/comani"
    expected = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source.rglob("*.py")}
    installed = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in package.rglob("*.py")}
    assert installed == expected, "Wheel test environment is stale; run scripts/refresh_wheel.py"
    help_result = subprocess.run(
        [str(python), "-B", "-m", "comani", "daemon", "--help"], cwd=tmp_path,
        env=env, text=True, capture_output=True, check=True,
    )
    assert "--comfy-root" in help_result.stdout


async def test_built_wheel_client_runs_over_http_without_torch(native_http, tmp_path):
    _, url = native_http
    project = Path(__file__).resolve().parents[2]
    python = project / ".venv-wheel/bin/python"
    if not python.exists():
        pytest.skip("Install the built wheel into .venv-wheel first")
    script = """
import asyncio,importlib.util,json,sys
assert importlib.util.find_spec('torch') is None
from comani import Client
import comani
async def main():
    async with Client(sys.argv[1]) as remote:
        call=remote.python(lambda:{'ok':True})
        assert await (await call.output()).fetch()=={'ok':True}
        with remote.graph() as graph:
            a,=await remote.python(lambda:3)
            b=remote.python(lambda value:value*7,inputs={'value':a})
            graph.output('answer',b.output())
        run=remote.submit(graph)
        assert await (await run.output('answer')).fetch()==21
    print(json.dumps({'package':comani.__file__,'answer':21}))
asyncio.run(main())
"""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    child = await asyncio.create_subprocess_exec(
        str(python), "-B", "-c", script, url, cwd=tmp_path, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=20)
    assert child.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert Path(result["package"]).is_relative_to(project / ".venv-wheel")
    assert result["answer"] == 21
