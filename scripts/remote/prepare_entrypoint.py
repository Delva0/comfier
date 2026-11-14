"""Prepare and dry-run the explicit Docker args package; never rents a machine."""
import base64
import hashlib
import io
import json
import secrets
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

from vastai.api.instances import build_create_instance_payload

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / '.local/remote-acceptance'
REMOTE = '/workspace/comani-acceptance'
IMAGE = 'pytorch/pytorch:2.9.1-cuda13.0-cudnn9-runtime'
NAMES = ['entrypoint_boot.sh', 'bootstrap.sh', 'disk_budget.py', 'download_models.py',
         'models.json', 'hardware_preflight.py', 'probe_nvfp4.py',
         'staging_receiver.py', 'announce_tunnel.py']


def main():
    LOCAL.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    files = {name: (ROOT / 'scripts/remote' / name).read_bytes() for name in NAMES}
    for name in ['entrypoint_boot.sh', 'bootstrap.sh']:
        files[name] = files[name].replace(b'\r\n', b'\n')
    boot = {'run_id': run_id, 'file_sha256': {name: hashlib.sha256(value).hexdigest()
                                            for name, value in files.items()},
            'comfy_commit': 'eee59185ff4f43ae2d9ad122b245619d27d9daad',
            'expected_wheel_sha256': 'dc2e095acfc4a272042e8499c13c9cc3aef7b701f6ae4d9a48c8333a8b89bcc8'}
    token = secrets.token_urlsafe(48).encode()
    (LOCAL / 'service-token').write_bytes(token)
    (LOCAL / 'service-token').chmod(0o600)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        entries = {'next/scripts/remote/' + name: data for name, data in files.items()}
        entries.update({'boot.json': json.dumps(boot).encode(), 'service-token': token,
                        'next/runtime-constraints.txt': (ROOT / 'runtime-constraints.txt').read_bytes()})
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            archive.addfile(info, io.BytesIO(data))
    payload = buffer.getvalue()
    payload_sha = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode('ascii')
    launcher = f'''python3 - <<'COMANI_EXPLICIT_DOCKER_UNPACK'
import base64, io, os, pathlib, tarfile
root = pathlib.Path('{REMOTE}')
root.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode('{encoded}')), mode='r:gz') as archive:
    archive.extractall(root, filter='data')
(root / 'payload.sha256').write_text('{payload_sha}')
os.execv('/bin/bash', ['/bin/bash', str(root / 'next/scripts/remote/entrypoint_boot.sh')])
COMANI_EXPLICIT_DOCKER_UNPACK
'''
    assert len(launcher) <= 16384
    path = LOCAL / 'entrypoint-launcher.sh'
    path.write_text(launcher)
    path.chmod(0o600)
    (LOCAL / 'entrypoint-payload.tar.gz').write_bytes(payload)
    (LOCAL / 'entrypoint-payload.tar.gz').chmod(0o600)
    body = build_create_instance_payload(image=IMAGE, disk=70, label='dry-run-only',
                                        runtype='args', onstart_cmd='/bin/bash', args=['-c', launcher])
    assert body['runtype'] == 'args' and body['onstart'] == '/bin/bash'
    assert body['args'] == ['-c', launcher]
    assert not any('ssh' in str(value) for key, value in body.items() if key == 'runtype')
    subprocess.run(['bash', '-n', str(path)], check=True)
    with tempfile.TemporaryDirectory(prefix='entrypoint-dryrun-', dir=LOCAL) as temporary:
        destination = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            assert all(member.isfile() for member in archive.getmembers())
            archive.extractall(destination, filter='data')
        assert (destination / 'service-token').read_bytes() == token
        for name, expected in boot['file_sha256'].items():
            installed = destination / 'next/scripts/remote' / name
            assert hashlib.sha256(installed.read_bytes()).hexdigest() == expected
            if name.endswith('.py'):
                compile(installed.read_bytes(), str(installed), 'exec')
            if name.endswith('.sh'):
                subprocess.run(['bash', '-n', str(installed)], check=True)
    record = {'run_id': run_id, 'payload_sha256': payload_sha, 'payload_bytes': len(payload),
              'launcher_sha256': hashlib.sha256(launcher.encode()).hexdigest(),
              'launcher_bytes': len(launcher.encode()), 'image': IMAGE,
              'runtype': body['runtype'], 'entrypoint': body['onstart'],
              'args_shape': ['-c', '<explicit decode, then exec entrypoint_boot.sh>'],
              'file_sha256': boot['file_sha256'], 'dry_run_passed': True,
              'claim_boundary': 'local decode, syntax, file identity and official SDK body only'}
    (LOCAL / 'entrypoint.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
