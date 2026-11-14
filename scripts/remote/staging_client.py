"""Read preparation status or upload one hashed final wheel over task HTTPS."""
import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from endpoint_discovery import discover

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / '.local/remote-acceptance'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('operation', choices=['status', 'upload', 'resume'])
parser.add_argument('--wheel', type=Path)
parser.add_argument('--stage', choices=['bootstrap', 'download', 'probe', 'daemon'])
parser.add_argument('--brief', action='store_true')
args = parser.parse_args()
state = json.loads((LOCAL / 'rental.json').read_text())
logs = subprocess.run([str(ROOT / '.local/vast-tools/bin/vastai'), 'logs', str(state['instance_id']), '--tail', '700'], capture_output=True, text=True, timeout=30, check=True).stdout
boot = json.loads((LOCAL / 'entrypoint.json').read_text())
assert state['run_id'] == boot['run_id']
url = discover(logs, 'control', boot['run_id'])
token = (LOCAL / 'service-token').read_text().strip()
headers = {'Authorization': 'Bearer ' + token}
if args.operation == 'upload':
    if not args.wheel or not args.wheel.is_file():
        parser.error('--wheel must be an existing frozen wheel')
    wheel = args.wheel.read_bytes()
    manifest = {'wheel': args.wheel.name, 'sha256': hashlib.sha256(wheel).hexdigest()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr(args.wheel.name, wheel)
    body = buffer.getvalue()
    headers.update({'X-Content-SHA256': hashlib.sha256(body).hexdigest(), 'Content-Type': 'application/zip'})
    request = Request(url + '/upload', data=body, headers=headers, method='POST')
elif args.operation == 'resume':
    if not args.stage:
        parser.error('--stage is required for fixed resume')
    headers['Content-Type'] = 'application/json'
    request = Request(url + '/resume', data=json.dumps({'stage': args.stage}).encode(), headers=headers, method='POST')
else:
    request = Request(url + '/', headers=headers)
with urlopen(request, timeout=45) as response:
    result = json.load(response)
(LOCAL / ('staging-' + args.operation + '.json')).write_text(json.dumps(result, indent=2) + '\n')
(LOCAL / 'endpoint-control.json').write_text(json.dumps({'url': url, 'phase': 'control', 'run_id': boot['run_id']}, indent=2))
if args.brief and args.operation == 'status':
    result = {'stage': result['stage'],
              'models_verified': [m['name'] for m in result.get('model-verification.json', {}).get('models', [])],
              'bootstrap_tail': result.get('bootstrap.log', '')[-1500:],
              'download_tail': result.get('model-download.log', '')[-1500:]}
print(json.dumps(result, indent=2))
