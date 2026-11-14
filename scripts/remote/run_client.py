"""Run the real acceptance/example with the private task token, never echoing it."""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import comani

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / '.local/remote-acceptance'
WHEEL_SHA256 = '16a04c6a0afe494880c084039bffd1f02548d13e068c41240997f95972f005c6'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('operation', choices=['smoke', 'longwait', 'model', 'diagnostics', 'collect', 'progress', 'encoder', 'retry-download', 'configure'])
args = parser.parse_args()
assert '/.local/final-client/' in str(Path(comani.__file__).resolve()), comani.__file__
state = json.loads((LOCAL / 'rental.json').read_text())
logs = subprocess.run([str(ROOT / '.local/vast-tools/bin/vastai'), 'logs', str(state['instance_id']), '--tail', '1200'], capture_output=True, text=True, timeout=30, check=True).stdout
boot = json.loads((LOCAL / 'entrypoint.json').read_text())
assert state['run_id'] == boot['run_id']
spec = importlib.util.spec_from_file_location('acceptance_endpoint_discovery', ROOT / 'scripts/remote/endpoint_discovery.py')
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)
url = discovery.discover(logs, 'daemon', boot['run_id'])
(LOCAL / 'endpoint-daemon.json').write_text(json.dumps({'url': url, 'phase': 'final-daemon', 'run_id': boot['run_id']}, indent=2))
environment = dict(os.environ)
environment['COMANI_TOKEN'] = (LOCAL / 'service-token').read_text().strip()
environment['COMANI_URL'] = url
output = ROOT / 'artifacts/remote' / (str(state['instance_id']) + '-20261114')
output.mkdir(parents=True, exist_ok=True)
if args.operation == 'smoke':
    command = [str(ROOT / 'scripts/remote/acceptance.py'), '--url', url, '--output', str(output / 'smoke')]
elif args.operation == 'longwait':
    command = [str(ROOT / 'scripts/remote/long_wait.py'), '--url', url, '--report', str(output / 'long-wait.json')]
elif args.operation == 'model':
    command = [str(ROOT / 'examples/minimax_h3_remote.py'), '--output', str(output), '--wheel-sha256', WHEEL_SHA256]
elif args.operation == 'encoder':
    command = [str(ROOT / 'scripts/remote/encoder_check.py'), '--report', str(output / 'encoder-check.json')]
else:
    command = [str(ROOT / 'scripts/remote/runtime_checks.py'), args.operation, '--output', str(output)]
with (output / (args.operation + '.log')).open('w') as log:
    process = subprocess.Popen([sys.executable, '-I', '-B', *command], env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        log.write(line)
        log.flush()
        print(line, end='', flush=True)
    code = process.wait()
raise SystemExit(code)
