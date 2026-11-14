"""Manage only the single Vast instance recorded for this acceptance run.

The official CLI reads the user's normal local credential config. Neither the
API key nor this program is copied to the rented machine. No automatic top-up.
"""
import argparse
import ast
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.local/remote-acceptance'
STATE = STATE_DIR / 'rental.json'
CLI = ROOT / '.local/vast-tools/bin/vastai'
IMAGE = 'pytorch/pytorch:2.9.1-cuda13.0-cudnn9-runtime'

def now():
    return datetime.now(timezone.utc).isoformat()

def cli(*args, timeout=60):
    r = subprocess.run([str(CLI), *map(str, args), '--raw'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f'Official CLI operation {args[:2]} failed, exit {r.returncode}')
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        if r.stdout.startswith('Started. '):
            value = ast.literal_eval(r.stdout.removeprefix('Started. ').strip())
            if isinstance(value, dict):
                return value
        raise RuntimeError(f'Official CLI operation {args[:2]} did not return JSON') from None

def save(data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(STATE)

def recorded():
    if not STATE.is_file():
        raise RuntimeError('No acceptance rental recorded')
    return json.loads(STATE.read_text())

def own_instance(data):
    items = cli('show', 'instances')
    matches = [i for i in items if i.get('id') == data['instance_id']]
    if not matches:
        return None
    item = matches[0]
    if item.get('label') != data['label']:
        raise RuntimeError('Instance label mismatch; refusing mutation')
    return item

def destroy():
    data = recorded()
    instance = own_instance(data)
    if instance is not None:
        response = subprocess.run([str(CLI), 'destroy', 'instance', str(data['instance_id']), '--yes'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        response.check_returncode()
    for _ in range(12):
        if own_instance(data) is None:
            data['destroyed_at_utc'] = now()
            data['destroy_verified_absent'] = True
            data['elapsed_hours_upper_bound'] = (time.time() - data['created_epoch']) / 3600
            data['estimated_compute_storage_usd_upper_bound'] = data['elapsed_hours_upper_bound'] * data['quoted_dph_total']
            save(data)
            (STATE_DIR / f"rental-{data['instance_id']}.json").write_text(json.dumps(data, indent=2) + '\n')
            print(json.dumps(data, indent=2), flush=True)
            return
        time.sleep(5)
    raise RuntimeError('Destroyed instance still appears; verify billing immediately')

def monitor():
    data = recorded()
    while True:
        if time.time() >= data['destroy_deadline_epoch']:
            destroy()
            return
        current = recorded()
        if current.get('instance_id') != data.get('instance_id'):
            return
        if current.get('destroy_verified_absent'):
            return
        try:
            instance = own_instance(current)
            status = {k: instance.get(k) for k in ['id', 'actual_status', 'cur_state', 'dph_total', 'total_cost', 'disk_space']} if instance else {'absent': True}
            with (STATE_DIR / 'billing.jsonl').open('a') as f:
                f.write(json.dumps({'at_utc': now(), 'status': status}) + '\n')
        except Exception as e:
            with (STATE_DIR / 'billing.jsonl').open('a') as f:
                f.write(json.dumps({'at_utc': now(), 'error_type': type(e).__name__}) + '\n')
        time.sleep(30)

def create(offer_id, entrypoint_launcher):
    if STATE.exists() and not recorded().get('destroy_verified_absent'):
        raise RuntimeError('An uncleared rental record exists; never create a second instance')
    label = datetime.now(timezone.utc).strftime('comani-acceptance-%Y%m%d-%H%M%S')
    boot = json.loads((STATE_DIR / 'entrypoint.json').read_text())
    launcher = entrypoint_launcher.read_text()
    assert boot['dry_run_passed'] and boot['runtype'] == 'args'
    assert hashlib.sha256(launcher.encode()).hexdigest() == boot['launcher_sha256']
    previous = recorded() if STATE.exists() else {}
    before = cli('show', 'instances')
    available = cli('search', 'offers', 'gpu_name=RTX_4090 num_gpus=1 cpu_arch=amd64 cpu_ram>=48 cpu_ram<=72 gpu_ram<26 disk_space>=70 inet_down>=300 pcie_bw>=10 cuda_vers>=13.0 reliability>=0.98', '--storage', '70', '--limit', '100', '--order', 'dph_total')
    offers = [item for item in available if item.get('id') == offer_id]
    if len(offers) != 1:
        offers = [item for item in available if 60000 <= item['cpu_ram'] < 73000
                  and item['dph_total'] < 0.50 and item['inet_down_cost'] <= 0.01
                  and item['cuda_max_good'] >= 13.0
                  and item['id'] != previous.get('offer_id')]
        if not offers:
            raise RuntimeError('No current offer in the accepted hardware/price/network range')
        offers.sort(key=lambda item: item['dph_total'] * 4 + item['inet_down_cost'] * 45)
    offer = offers[0]
    offer_id = offer['id']
    print(json.dumps({'selected_offer': {k: offer[k] for k in ['id', 'gpu_name', 'gpu_ram', 'cpu_ram', 'dph_total', 'storage_total_cost', 'inet_down_cost', 'inet_up_cost', 'geolocation']}}), flush=True)
    if not (offer['gpu_name'] == 'RTX 4090' and offer['num_gpus'] == 1 and 24000 < offer['gpu_ram'] < 26000 and 48000 <= offer['cpu_ram'] < 73000 and offer['dph_total'] < 0.50):
        raise RuntimeError('Offer changed outside the selected configuration/price')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    deadline = min(started + 3.5 * 3600, datetime(2026, 11, 14, 18, 40, tzinfo=timezone.utc).timestamp())
    if deadline - started < 1800:
        raise RuntimeError('Less than 30 minutes remain before the acceptance safety deadline')
    data = {
        'label': label, 'offer_id': offer_id, 'created_epoch': started,
        'machine_id': offer.get('machine_id'),
        'created_at_utc': now(), 'destroy_deadline_epoch': deadline,
        'destroy_deadline_utc': datetime.fromtimestamp(deadline, timezone.utc).isoformat(),
        'run_id': boot['run_id'], 'launch_mode': 'args',
        'entrypoint_launcher_sha256': boot['launcher_sha256'],
        'entrypoint_payload_sha256': boot['payload_sha256'],
        'image': IMAGE, 'requested_disk_gib': 70,
        'quoted_dph_total': offer['dph_total'],
        'quoted_dph_compute': offer['dph_base'],
        'quoted_dph_storage': offer.get('storage_total_cost'),
        'quoted_ingress_usd_per_gb': offer['inet_down_cost'],
        'quoted_egress_usd_per_gb': offer['inet_up_cost'],
        'cpu_ram_mib': offer['cpu_ram'], 'gpu_ram_mib': offer['gpu_ram'],
        'existing_instance_ids': [i['id'] for i in before],
    }
    save(data)
    # Do not retry create: an ambiguous response must be reconciled by unique label.
    try:
        from vastai import VastAI
        response = VastAI().create_instance(id=offer_id, image=IMAGE, disk=70,
                                            runtype='args', onstart_cmd='/bin/bash',
                                            args=['-c', launcher], label=label, cancel_unavail=True)
        if not response.get('success') or not response.get('new_contract'):
            raise RuntimeError('Vast rental response did not confirm creation')
        data['instance_id'] = response['new_contract']
    except Exception:
        matches = [i for i in cli('show', 'instances') if i.get('label') == label]
        if len(matches) != 1:
            data['creation_outcome'] = 'uncertain; reconcile unique label before any new rental'
            save(data)
            raise
        data['instance_id'] = matches[0]['id']
    save(data)
    log = (STATE_DIR / 'watchdog.log').open('ab', buffering=0)
    process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'monitor'], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    data['watchdog_pid'] = process.pid
    data['ssh_requested'] = False
    save(data)
    print(json.dumps(data, indent=2), flush=True)

def status():
    data = recorded()
    item = own_instance(data)
    if item:
        fields = ['id', 'label', 'actual_status', 'cur_state', 'status_msg', 'ssh_host', 'ssh_port', 'public_ipaddr', 'ports', 'dph_total', 'total_cost', 'disk_space', 'start_date']
        print(json.dumps({k: item.get(k) for k in fields}, indent=2))
    else:
        print(json.dumps({'recorded_instance_absent': True, 'record': data}, indent=2))

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('operation', choices=['create', 'status', 'destroy', 'monitor'])
p.add_argument('--offer', type=int)
p.add_argument('--entrypoint-launcher', type=Path)
a = p.parse_args()
if a.operation == 'create':
    if not a.offer:
        p.error('--offer is required')
    if not a.entrypoint_launcher:
        p.error('--entrypoint-launcher is required; no SSH wrapper launch is allowed')
    create(a.offer, a.entrypoint_launcher)
elif a.operation == 'status':
    status()
elif a.operation == 'destroy':
    destroy()
else:
    monitor()
