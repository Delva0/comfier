"""Start, inspect, or stop only this project's recorded loopback test daemon."""

import argparse
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
STATE = PROJECT / ".local" / "daemon.json"


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        return fields[21] if fields[2] != "Z" else None
    except FileNotFoundError:
        return None


def recorded():
    return json.loads(STATE.read_text()) if STATE.exists() else None


def alive(record):
    return record and process_identity(record["pid"]) == record["identity"]


def wait_ready(child, record, *, timeout):
    deadline = time.monotonic() + timeout
    headers = {}
    if os.environ.get("COMANI_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["COMANI_TOKEN"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = "The daemon has not begun listening"
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Daemon exited with code {child.returncode}; inspect {record['log']}")
        try:
            request = urllib.request.Request(record["url"] + "/queue", headers=headers)
            with opener.open(request, timeout=min(0.5, max(0.01, deadline - time.monotonic()))) as response:
                payload = json.load(response)
            if "queue_running" in payload and "queue_pending" in payload:
                return
            last_error = "The daemon returned an unexpected readiness response"
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            last_error = str(error)
        time.sleep(0.1)
    raise RuntimeError(f"Daemon was not ready within {timeout:g}s: {last_error}; inspect {record['log']}")


def stop_failed_start(child, record):
    if child.poll() is not None or not alive(record):
        return
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if alive(record):
            child.kill()
        child.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("--comfy-root", type=Path)
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--cpu", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=120,
                        help="Maximum seconds to wait for the native runtime HTTP readiness check")
    args = parser.parse_args()
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout must be positive")
    previous = recorded()
    if args.action == "status":
        print(json.dumps({"running": bool(alive(previous)), "record": previous}, indent=2))
        return
    if args.action == "stop":
        if not alive(previous):
            print("The recorded daemon is not running; no process was signalled.")
            return
        os.kill(previous["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 30
        while alive(previous) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(previous):
            raise RuntimeError("Recorded daemon is still shutting down; inspect its log")
        print(f"Stopped recorded PID {previous['pid']}")
        return
    if alive(previous):
        parser.error(f"A project daemon is already recorded at PID {previous['pid']}")
    if args.comfy_root is None or not (args.comfy_root / "execution.py").is_file():
        parser.error("start requires --comfy-root pointing to ComfyUI")
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            parser.error(f"Port {args.port} is occupied; no existing service was changed")
    python = PROJECT / ".venv/bin/python"
    command = [str(python), "-B", "-m", "comani._runtime", "--comfy-root",
               str(args.comfy_root.resolve()), "--host", "127.0.0.1", "--port",
               str(args.port), "--no-custom-nodes"]
    if args.cpu:
        command.append("--cpu")
    STATE.parent.mkdir(exist_ok=True)
    log = STATE.parent / "daemon.log"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    with log.open("ab") as output:
        child = subprocess.Popen(command, cwd=args.comfy_root, env=env,
                                 stdin=subprocess.DEVNULL, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    record = {"pid": child.pid, "identity": process_identity(child.pid),
              "url": f"http://127.0.0.1:{args.port}", "log": str(log),
              "comfy_root": str(args.comfy_root.resolve()), "cpu": args.cpu,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ready": False}
    STATE.write_text(json.dumps(record, indent=2) + "\n")
    try:
        wait_ready(child, record, timeout=args.startup_timeout)
    except BaseException as error:
        stop_failed_start(child, record)
        record["startup_error"] = str(error)
        STATE.write_text(json.dumps(record, indent=2) + "\n")
        raise
    record["ready"] = True
    STATE.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
