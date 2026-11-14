"""A bounded native CPU fault-injection check, isolated from the GPU daemon."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is None
    or not (Path(os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")) / "execution.py").is_file(),
    reason="Requires the optional native CPU test environment",
)
def test_cache_loss_settles_and_empty_queue_does_not_mean_worker_finished():
    script = Path(__file__).with_name("cache_settlement_probe.py")
    completed = subprocess.run(
        [sys.executable, "-u", str(script)], text=True, capture_output=True, timeout=20,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(report, sort_keys=True))
    assert report["eviction"]["outcome"] in {"ready", "Expired", "Skipped", "RemoteError"}
    assert report["missed_capture"]["outcome"] in {"Skipped", "NotProduced", "RemoteError"}
    closed = report["closed_session"]
    assert closed["queue_size"] == 0 and closed["whole_done"]
    assert closed["active"] and closed["inflight"] and not closed["native_ended"]
    assert closed["status"] == "cancelled" and closed["eventually_finished"]
