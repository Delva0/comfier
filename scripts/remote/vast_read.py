"""Sanitized official Vast CLI reads for this independent acceptance run."""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

cli = Path(__file__).resolve().parents[2] / '.local/vast-tools/bin/vastai'
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('operation', choices=['user', 'offers', 'instances'])
a = p.parse_args()
commands = {
    'user': ['show', 'user'],
    'offers': ['search', 'offers', 'gpu_name=RTX_4090 num_gpus=1 cpu_ram>=48 cpu_ram<=72 gpu_ram<26 disk_space>=70 inet_down>=300 cuda_vers>=13.0 reliability>=0.98', '--limit', '5', '--storage', '70', '--order', 'dph_total'],
    'instances': ['show', 'instances'],
}
r = subprocess.run([str(cli), '--raw', *commands[a.operation]], capture_output=True, text=True)
if r.returncode:
    print(json.dumps({'operation': a.operation, 'returncode': r.returncode, 'error': 'official CLI request failed; inspect locally without revealing credentials'}))
    raise SystemExit(1)
try:
    data = json.loads(r.stdout)
except json.JSONDecodeError:
    print(json.dumps({'operation': a.operation, 'error': 'official CLI did not return JSON'}))
    raise SystemExit(1)
if a.operation == 'user':
    allowed = ['id', 'balance', 'credit', 'credits', 'balance_threshold', 'billable_credit', 'got_signup_credit', 'is_email_verified']
    result = {k: data[k] for k in allowed if k in data}
    result['authenticated'] = bool(data.get('id'))
elif a.operation == 'offers':
    allowed = ['id', 'machine_id', 'gpu_name', 'gpu_ram', 'num_gpus', 'cpu_ram', 'cpu_cores_effective', 'cpu_name', 'disk_space', 'disk_bw', 'dph_base', 'dph_total', 'storage_total_cost', 'storage_cost', 'inet_down', 'inet_up', 'inet_down_cost', 'inet_up_cost', 'reliability', 'verification', 'driver_version', 'cuda_max_good', 'pcie_bw', 'geolocation', 'direct_port_count']
    result = [{k: offer[k] for k in allowed if k in offer} for offer in data]
else:
    allowed = ['id', 'label', 'actual_status', 'cur_state', 'gpu_name', 'num_gpus', 'dph_total', 'disk_space', 'start_date', 'end_date']
    result = [{k: instance[k] for k in allowed if k in instance} for instance in data]
print(json.dumps({'at_utc': datetime.now(timezone.utc).isoformat(), 'operation': a.operation, 'result': result}, indent=2))
