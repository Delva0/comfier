"""Temporary token-authenticated wheel upload during HTTPS-only bootstrap.

It stores a wheel after two SHA256 checks. It does not execute uploaded commands.
The explicit entrypoint installs that wheel; this fixed control channel stays alive.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path('/workspace/comani-acceptance')
TOKEN = (ROOT / 'service-token').read_text().strip()
JOB_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def authenticated(self):
        return secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + TOKEN)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authenticated():
            return self.send_json(401, {'error': 'unauthorized'})
        result = {'stage': 'preparing environment/models or waiting final wheel', 'receiver_pid': os.getpid(),
                  'disk_bytes': dict(zip(['total', 'used', 'free'], shutil.disk_usage(ROOT)))}
        result['script_sha256'] = {}
        for name in ['probe_nvfp4.py', 'bootstrap.sh', 'hardware_preflight.py']:
            path = ROOT / 'next/scripts/remote' / name
            if path.is_file():
                result['script_sha256'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for name in ['boot.json', 'hardware.json', 'model-verification.json', 'upload-ready.json', 'endpoint-daemon.json']:
            path = ROOT / name
            if path.exists():
                result[name] = json.loads(path.read_text())
        ready = ROOT / 'next/.local/runtime-ready.json'
        if ready.exists():
            result['runtime-ready.json'] = json.loads(ready.read_text())
        for name in ['bootstrap.log', 'model-download.log', 'hardware.log', 'probe.log', 'daemon.log', 'wheel-install.log']:
            path = ROOT / name
            if path.exists():
                result[name] = path.read_text(errors='replace')[-4000:]
        self.send_json(200, result)

    def do_POST(self):
        if not self.authenticated():
            return self.send_json(401, {'error': 'unauthorized'})
        if self.path == '/resume':
            return self.resume()
        if self.path != '/upload':
            return self.send_json(404, {'error': 'unknown endpoint'})
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 32 << 20:
            return self.send_json(413, {'error': 'upload must be <=32MiB'})
        body = self.rfile.read(length)
        if hashlib.sha256(body).hexdigest() != self.headers.get('X-Content-SHA256'):
            return self.send_json(400, {'error': 'upload hash mismatch'})
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                name = manifest['wheel']
                if not re.fullmatch(r'[A-Za-z0-9_.+\-]+\.whl', name):
                    raise ValueError('invalid wheel filename')
                wheel = archive.read(name)
                actual = hashlib.sha256(wheel).hexdigest()
                if actual != manifest['sha256']:
                    raise ValueError('wheel hash mismatch')
                destination = ROOT / 'next/dist' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix('.upload')
                temporary.write_bytes(wheel)
                temporary.replace(destination)
                record = {'wheel': name, 'sha256': actual, 'bytes': len(wheel)}
                pending = ROOT / 'upload-ready.tmp'
                pending.write_text(json.dumps(record))
                pending.replace(ROOT / 'upload-ready.json')
        except (ValueError, KeyError, zipfile.BadZipFile):
            return self.send_json(400, {'error': 'invalid wheel upload'})
        self.send_json(200, record)

    def resume(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= 128:
            return self.send_json(400, {'error': 'invalid fixed action'})
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {'error': 'invalid JSON'})
        if not isinstance(payload, dict) or set(payload) != {'stage'}:
            return self.send_json(400, {'error': 'only the fixed stage name is accepted'})
        stage = payload['stage']
        if not isinstance(stage, str):
            return self.send_json(400, {'error': 'stage must be a fixed name'})
        actions = {
            'bootstrap': (['bash', str(ROOT / 'next/scripts/remote/bootstrap.sh')], 'bootstrap.log', 'bootstrap.sh'),
            'download': (['python3', '-u', str(ROOT / 'next/scripts/remote/download_models.py'), '--comfy-root', str(ROOT / 'ComfyUI'), '--report', str(ROOT / 'model-verification.json')], 'model-download.log', 'download_models.py'),
            'probe': ([str(ROOT / 'next/.venv/bin/python'), '-B', str(ROOT / 'next/scripts/remote/probe_nvfp4.py'), '--comfy-root', str(ROOT / 'ComfyUI')], 'probe.log', 'probe_nvfp4.py'),
        }
        if stage == 'daemon':
            (ROOT / 'restart-daemon').touch()
            return self.send_json(202, {'fixed_action_requested': stage})
        if stage not in actions:
            return self.send_json(400, {'error': 'unknown fixed action'})
        command, logfile, identity = actions[stage]
        with JOB_LOCK:
            pidfile = ROOT / (stage + '.pid')
            if pidfile.exists():
                pid = int(pidfile.read_text())
                process = Path('/proc') / str(pid) / 'cmdline'
                if process.exists() and identity.encode() in process.read_bytes():
                    return self.send_json(409, {'error': 'this fixed action is already running'})
            if stage == 'bootstrap':
                (ROOT / 'next/.local/bootstrap.exit').unlink(missing_ok=True)
                (ROOT / 'next/.local/runtime-ready.json').unlink(missing_ok=True)
            with (ROOT / logfile).open('ab') as output:
                child = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=output, start_new_session=True)
            pidfile.write_text(str(child.pid))
        self.send_json(202, {'fixed_action_started': stage, 'pid': child.pid})


ThreadingHTTPServer(('127.0.0.1', 8199), Handler).serve_forever()
