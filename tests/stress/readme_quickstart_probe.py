"""Execute root README's launch/example/stop sequence with the CPU option.

Uses the already provisioned .venv. It does not re-bootstrap dependencies.
"""
import argparse
import json
import os
import socket
import subprocess
import time
from pathlib import Path


def main(args):
    project = Path(__file__).resolve().parents[2]
    python = project / ".venv/bin/python"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", args.port)), "port already occupied; no service touched"
    launcher = [str(python), "-B", "scripts/dev_server.py"]
    report = {"scope": "root README quickstart using existing runtime environment; --cpu documented in development guide", "steps": []}
    recorded_pid = None

    def run(command):
        started = time.perf_counter()
        result = subprocess.run(command, cwd=project, env=env, text=True, capture_output=True, timeout=45)
        row = {"command": command, "returncode": result.returncode, "seconds": round(time.perf_counter() - started, 3),
               "stdout": result.stdout, "stderr": result.stderr}
        report["steps"].append(row)
        return row

    try:
        start = run(launcher + ["start", "--comfy-root", "/home/delva/projects/ComfyUI", "--port", str(args.port), "--cpu"])
        if start["returncode"] == 0:
            recorded_pid = json.loads(start["stdout"])["pid"]
            run([str(python), "-B", "scripts/example.py", "--url", f"http://127.0.0.1:{args.port}"])
    finally:
        if recorded_pid is not None:
            record = json.loads((project / ".local/daemon.json").read_text())
            assert record["pid"] == recorded_pid, "record changed; refusing to stop another process"
            run(launcher + ["stop"])
        report["passed"] = len(report["steps"]) == 3 and all(row["returncode"] == 0 for row in report["steps"])
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/readme_quickstart_results.json"))
    raise SystemExit(main(parser.parse_args()))
