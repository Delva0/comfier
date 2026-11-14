#!/usr/bin/env bash
# Run only inside the newly rented acceptance container, from the synced next/ root.
set -euo pipefail
export UV_NO_CACHE=1
export PYTHONUNBUFFERED=1
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$project_root/.local"
trap 'result=$?; printf "%s\n" "$result" > "$project_root/.local/bootstrap.exit"' EXIT
rm -f "$project_root/.local/runtime-ready.json" "$project_root/.local/bootstrap.exit"
comfy_root="${COMANI_COMFY_ROOT:-/workspace/comani-acceptance/ComfyUI}"
comfy_commit=eee59185ff4f43ae2d9ad122b245619d27d9daad
apt-get update -qq
apt-get install -y -qq git ca-certificates libglib2.0-0 libgl1 >/dev/null
python3 -m pip install --disable-pip-version-check --no-cache-dir uv
uv python install 3.11.11
if [[ ! -f "$comfy_root/execution.py" ]]; then
  mkdir -p "$comfy_root"
  git -C "$comfy_root" init -q
  git -C "$comfy_root" remote add origin https://github.com/Comfy-Org/ComfyUI.git
  git -C "$comfy_root" fetch -q --depth 1 origin "$comfy_commit"
  git -C "$comfy_root" checkout -q --detach FETCH_HEAD
fi
[[ "$(git -C "$comfy_root" rev-parse HEAD)" == "$comfy_commit" ]]
[[ -z "$(git -C "$comfy_root" status --porcelain)" ]]
if [[ ! -x "$project_root/.venv/bin/python" ]]; then
  uv venv --python 3.11.11 "$project_root/.venv"
fi
uv pip install --python "$project_root/.venv/bin/python" \
  --index-url https://download.pytorch.org/whl/cu130 \
  torch==2.9.1 torchvision==0.24.1
uv pip install --python "$project_root/.venv/bin/python" \
  --constraint "$project_root/runtime-constraints.txt" \
  --requirement "$comfy_root/requirements.txt" \
  cloudpickle==3.1.2 aiohttp
if [[ -d "$project_root/src/comani" ]]; then
  uv pip install --python "$project_root/.venv/bin/python" --editable "$project_root[test]"
fi
uv pip install --python "$project_root/.venv/bin/python" opencv-python-headless
uv pip check --python "$project_root/.venv/bin/python"
if [[ -d "$project_root/src/comani" ]]; then
  "$project_root/.venv/bin/python" -B "$project_root/scripts/check_environment.py" --comfy-root "$comfy_root" --cuda
fi
"$project_root/.venv/bin/python" -c 'import json,platform,cloudpickle,torch; print(json.dumps(dict(python=platform.python_version(),cloudpickle=cloudpickle.__version__,torch=torch.__version__)))' > "$project_root/.local/runtime-ready.json"
# Optional diagnostics are run separately after the daemon/control channel starts.
