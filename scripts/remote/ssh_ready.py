"""Probe only provider-reported endpoints of the one recorded instance."""
import argparse
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ssh_proxy import connect

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--proxy', action='store_true')
args = parser.parse_args()

root = Path(__file__).resolve().parents[2]
directory = root / '.local/remote-acceptance'
state = json.loads((directory / 'rental.json').read_text())
result = subprocess.run([str(root / '.local/vast-tools/bin/vastai'), 'show', 'instances', '--raw'], capture_output=True, text=True, check=True)
items = [item for item in json.loads(result.stdout) if item.get('id') == state['instance_id']]
assert len(items) == 1 and items[0]['label'] == state['label']
instance = items[0]
endpoints = [(instance['ssh_host'], instance['ssh_port'])]
for port in instance.get('ports', {}).get('22/tcp', []) if instance.get('ports') else []:
    endpoint = (instance['public_ipaddr'], int(port['HostPort']))
    if endpoint not in endpoints:
        endpoints.append(endpoint)

def probe(endpoint):
    try:
        connection_factory = (lambda address, timeout: connect(*address, timeout=timeout)) if args.proxy else socket.create_connection
        with connection_factory(endpoint, timeout=4) as connection:
            connection.settimeout(4)
            connection.sendall(b'SSH-2.0-comani_readiness_probe\r\n')
            banner = connection.recv(256).decode('ascii', errors='replace').strip()
            return {'endpoint': endpoint, 'banner': banner[:160], 'ready': banner.startswith('SSH-')}
    except Exception as error:
        return {'endpoint': endpoint, 'ready': False, 'error': type(error).__name__}

with ThreadPoolExecutor(max_workers=2) as pool:
    probes = list(pool.map(probe, endpoints))
print(json.dumps({'instance_id': instance['id'], 'actual_status': instance.get('actual_status'), 'probes': probes}, indent=2))
ready = [item for item in probes if item['ready']]
if ready:
    endpoint = ready[-1]['endpoint']
    config = '\n'.join([
        'Host comani-acceptance', f'    HostName {endpoint[0]}', f'    Port {endpoint[1]}',
        '    User root', f"    IdentityFile {state['private_key_path_local_only']}",
        '    IdentitiesOnly yes', '    BatchMode yes', '    ConnectTimeout 15',
        '    ServerAliveInterval 30', '    ServerAliveCountMax 3',
        '    StrictHostKeyChecking accept-new',
        f'    UserKnownHostsFile {directory / "known_hosts"}', '',
    ])
    if args.proxy:
        config += f'    ProxyCommand {root / ".local/vast-tools/bin/python"} {Path(__file__).with_name("ssh_proxy.py")} %h %p\n'
    (directory / 'ssh_config').write_text(config)
    subprocess.run(['ssh', '-F', str(directory / 'ssh_config'), 'comani-acceptance', 'nvidia-smi'], check=True, timeout=30)
else:
    raise SystemExit(2)
