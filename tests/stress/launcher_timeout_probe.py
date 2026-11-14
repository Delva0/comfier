"""A failed readiness deadline must terminate only its recorded child."""
import json
import os
import socket
import subprocess
from pathlib import Path

project = Path(__file__).resolve().parents[2]
with socket.socket() as probe:
    assert probe.connect_ex(("127.0.0.1", 8190)), "port occupied; no service touched"
command = [str(project / ".venv/bin/python"), "-B", "scripts/dev_server.py", "start",
           "--comfy-root", "/home/delva/projects/ComfyUI", "--port", "8190", "--cpu", "--startup-timeout", ".001"]
env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
env.pop("PYTHONPATH", None)
result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, timeout=15)
record = json.loads((project / ".local/daemon.json").read_text())
path = Path(f"/proc/{record['pid']}/stat")
alive = path.exists() and path.read_text().split()[2] != "Z"
with socket.socket() as probe:
    listening = probe.connect_ex(("127.0.0.1", 8190)) == 0
assert result.returncode != 0 and not record["ready"] and record.get("startup_error") and not alive and not listening
report = {"passed": True, "start_returncode": result.returncode, "recorded_pid": record["pid"],
          "ready": record["ready"], "startup_error": record["startup_error"],
          "recorded_child_alive": alive, "port_listening": listening}
(project / "tests/stress/launcher_timeout_results.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report), flush=True)
