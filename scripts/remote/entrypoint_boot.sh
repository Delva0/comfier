#!/bin/bash
# Explicit Docker args entrypoint. No SSH wrapper or provider onstart update.
set -u
root=/workspace/comani-acceptance
cd "$root"
run_id="$(python3 -c 'import json; print(json.load(open("boot.json"))["run_id"])')"
echo "COMANI_ENTRYPOINT_RUN_ID_$run_id"
echo "COMANI_ENTRYPOINT_PAYLOAD_SHA256_$(cat payload.sha256)"
python3 - <<'PY'
import hashlib, json
from pathlib import Path
root = Path('/workspace/comani-acceptance')
for name in ['entrypoint_boot.sh','bootstrap.sh','probe_nvfp4.py']:
    path = root / 'next/scripts/remote' / name
    print(json.dumps({'boot_file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}), flush=True)
PY
# Only Python from the official PyTorch image is required to start control.
python3 - <<'PY'
import hashlib, time
from pathlib import Path
from urllib.request import urlopen
url = 'https://github.com/cloudflare/cloudflared/releases/download/2026.11.0/cloudflared-linux-amd64'
target = Path('/workspace/comani-acceptance/cloudflared')
expected = '51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd'
for attempt in range(5):
    try:
        with urlopen(url, timeout=45) as source, target.open('wb') as output:
            while chunk := source.read(1 << 20):
                output.write(chunk)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
        target.chmod(0o700)
        break
    except Exception:
        if attempt == 4: raise
        time.sleep(2)
PY
if [[ $? != 0 ]]; then echo COMANI_CONTROL_BINARY_FAILED; sleep 3600; exit 1; fi
python3 -u next/scripts/remote/staging_receiver.py >receiver.log 2>&1 &
receiver_pid=$!
rm -f endpoint-control.json endpoint-daemon.json
: >control-tunnel.log
: >daemon-tunnel.log
./cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8199 >control-tunnel.log 2>&1 &
control_pid=$!
python3 -u next/scripts/remote/announce_tunnel.py control --log "$root/control-tunnel.log" --run-id "$run_id" &
echo COMANI_CONTROL_PROCESS_STARTED
touch bootstrap.log model-download.log daemon.log wheel-install.log probe.log
tail --pid=$$ -n +1 -F bootstrap.log model-download.log daemon.log wheel-install.log probe.log &
python3 -u next/scripts/remote/hardware_preflight.py >hardware.log 2>&1
if [[ $? == 0 ]]; then
  python3 -u next/scripts/remote/download_models.py --comfy-root "$root/ComfyUI" --report "$root/model-verification.json" >model-download.log 2>&1 &
  echo $! >download.pid
else
  echo COMANI_RESOURCE_REVIEW_REQUIRED_CONTROL_STAYS_ALIVE
fi
mkdir -p next/.local
rm -f next/.local/bootstrap.exit next/.local/runtime-ready.json
bash next/scripts/remote/bootstrap.sh >bootstrap.log 2>&1 &
echo $! >bootstrap.pid
installed_sha=""
daemon_pid=""
daemon_tunnel_pid=""
while kill -0 "$receiver_pid" 2>/dev/null; do
  bootstrap_pid="$(cat bootstrap.pid 2>/dev/null || true)"
  bootstrap_running=false
  if [[ -n "$bootstrap_pid" && -r "/proc/$bootstrap_pid/stat" ]]; then
    read -r process_id process_name bootstrap_state process_tail <"/proc/$bootstrap_pid/stat"
    [[ "$bootstrap_state" == Z ]] || bootstrap_running=true
  fi
  if [[ -n "$bootstrap_pid" && "$bootstrap_running" == false && -f next/.local/bootstrap.exit && "$(cat next/.local/bootstrap.exit)" == 0 && -f upload-ready.json ]]; then
    wanted_sha="$(python3 -c 'import json; print(json.load(open("upload-ready.json"))["sha256"])')"
    if [[ "$wanted_sha" != "$installed_sha" || -f restart-daemon ]]; then
      rm -f restart-daemon
      [[ -z "$daemon_pid" ]] || kill "$daemon_pid" 2>/dev/null || true
      [[ -z "$daemon_tunnel_pid" ]] || kill "$daemon_tunnel_pid" 2>/dev/null || true
      wheel_name="$(python3 -c 'import json; print(json.load(open("upload-ready.json"))["wheel"])')"
      python3 -c 'import hashlib,sys; assert hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest() == sys.argv[2]' "next/dist/$wheel_name" "$wanted_sha"
      if [[ $? != 0 ]]; then echo COMANI_WHEEL_HASH_REVIEW_REQUIRED; sleep 10; continue; fi
      uv pip install --python next/.venv/bin/python --reinstall --no-deps "next/dist/$wheel_name" >wheel-install.log 2>&1
      if [[ $? == 0 ]]; then
        COMANI_TOKEN="$(cat service-token)" next/.venv/bin/python -B -m comani._runtime --comfy-root "$root/ComfyUI" --host 127.0.0.1 --port 8190 --no-custom-nodes >daemon.log 2>&1 &
        daemon_pid=$!
        echo "$daemon_pid" >daemon.pid
        rm -f endpoint-daemon.json
        : >daemon-tunnel.log
        ./cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8190 >daemon-tunnel.log 2>&1 &
        daemon_tunnel_pid=$!
        python3 -u next/scripts/remote/announce_tunnel.py daemon --log "$root/daemon-tunnel.log" --run-id "$run_id" &
        installed_sha="$wanted_sha"
        echo "COMANI_FINAL_DAEMON_LAUNCHED_WHEEL_SHA256_$installed_sha"
      else
        echo COMANI_WHEEL_INSTALL_REVIEW_REQUIRED_CONTROL_STAYS_ALIVE
        sleep 10
      fi
    fi
  fi
  sleep 2
done
echo COMANI_CONTROL_EXITED
wait
