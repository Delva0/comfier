"""Deploy this task's code through Vast's documented onstart update API.

Local-only: the Vast key stays in the official local SDK configuration. The
payload contains only this project and a newly generated service token.
"""
import argparse
import base64
import hashlib
import io
import json
import secrets
import tarfile
from pathlib import Path

from vastai import VastAI

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / '.local/remote-acceptance'
REMOTE = '/workspace/comani-acceptance'
REMOTE_SCRIPTS = ['bootstrap.sh', 'download_models.py', 'models.json',
                  'hardware_preflight.py', 'probe_nvfp4.py', 'staging_receiver.py']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    state = json.loads((LOCAL / 'rental.json').read_text())
    client = VastAI()
    instances = client.show_instances()
    matches = [item for item in instances if item['id'] == state['instance_id']]
    assert len(matches) == 1 and matches[0]['label'] == state['label']
    token_path = LOCAL / 'service-token'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(48))
        token_path.chmod(0o600)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in ['runtime-constraints.txt']:
            archive.add(ROOT / name, arcname='next/' + name)
        for name in REMOTE_SCRIPTS:
            archive.add(ROOT / 'scripts/remote' / name, arcname='next/scripts/remote/' + name)
        archive.add(token_path, arcname='service-token')
    payload = buffer.getvalue()
    encoded = base64.b64encode(payload).decode('ascii')
    script = f'''#!/usr/bin/env bash
set -euo pipefail
mkdir -p {REMOTE}
cd {REMOTE}
exec > >(tee -a onstart.log) 2>&1
echo COMANI_ONSTART_ENTERED
echo COMANI_ONSTART_PAYLOAD_SHA256_{hashlib.sha256(payload).hexdigest()}
base64 --decode <<'COMANI_PAYLOAD' | tar -xzf - -C {REMOTE}
{encoded}
COMANI_PAYLOAD
chmod 600 service-token
python3 next/scripts/remote/hardware_preflight.py
echo COMANI_RESOURCE_PREFLIGHT_PASSED
curl --fail --location --retry 3 --output cloudflared https://github.com/cloudflare/cloudflared/releases/download/2026.11.0/cloudflared-linux-amd64
echo '51f7c68b57ed7269164075d7e789efce5b59355982f06392deeb037d982a51fd  cloudflared' | sha256sum --check
chmod 700 cloudflared
python3 -u next/scripts/remote/staging_receiver.py > receiver.log 2>&1 &
receiver_pid=$!
./cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8199 > staging-tunnel.log 2>&1 &
tunnel_pid=$!
python3 -u next/scripts/remote/download_models.py --comfy-root {REMOTE}/ComfyUI --report {REMOTE}/model-verification.json > model-download.log 2>&1 &
model_pid=$!
bash next/scripts/remote/bootstrap.sh > bootstrap.log 2>&1 &
bootstrap_pid=$!
touch model-download.log bootstrap.log staging-tunnel.log daemon.log
tail --pid=$$ -n +1 -F model-download.log bootstrap.log staging-tunnel.log daemon.log &
wait "$bootstrap_pid"
echo COMANI_BOOTSTRAP_PASSED
wait "$model_pid"
echo COMANI_ALL_MODEL_HASHES_VERIFIED
while [[ ! -f upload-ready.json ]]; do sleep 2; done
wheel_name="$(python3 -c 'import json; print(json.load(open("upload-ready.json"))["wheel"])')"
uv pip install --python next/.venv/bin/python "next/dist/$wheel_name"
next/.venv/bin/python -c 'import comani; print(comani.__file__)'
echo COMANI_FINAL_WHEEL_INSTALLED
kill "$receiver_pid" "$tunnel_pid"
export COMANI_TOKEN="$(cat service-token)"
cd next
.venv/bin/python -B -m comani._runtime --comfy-root {REMOTE}/ComfyUI --host 127.0.0.1 --port 8190 --no-custom-nodes > {REMOTE}/daemon.log 2>&1 &
echo $! > {REMOTE}/daemon.pid
cd {REMOTE}
echo COMANI_STARTING_AUTHENTICATED_HTTPS_TUNNEL
./cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:8190
'''
    path = LOCAL / 'onstart.sh'
    path.write_text(script)
    path.chmod(0o600)
    assert len(script) <= 16384, 'Provider onstart field limit exceeded'
    record = {'instance_id': state['instance_id'], 'payload_sha256': hashlib.sha256(payload).hexdigest(),
              'payload_bytes': len(payload), 'onstart_bytes': len(script.encode()),
              'cloudflared_version': '2026.11.0', 'mode': 'strong-token temporary HTTPS',
              'final_frozen_wheel_acceptance': False}
    (LOCAL / 'deployment.json').write_text(json.dumps(record, indent=2) + '\n')
    if args.apply:
        try:
            result = client.update_instance(id=state['instance_id'], image=state['image'], onstart=script)
        except Exception as error:
            response = getattr(error, 'response', None)
            if response is not None:
                try:
                    details = response.json()
                except ValueError:
                    details = {'message': response.text[:400]}
                safe = {key: str(details[key])[:700].replace(token_path.read_text(), '[service-token]')
                        for key in ['error', 'msg', 'message', 'detail'] if key in details}
                print(json.dumps({'provider_http_status': response.status_code, 'details': safe}), flush=True)
            raise
        assert result.get('success'), 'Provider did not acknowledge onstart update'
        record['provider_update_acknowledged'] = True
        (LOCAL / 'deployment.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
