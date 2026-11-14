"""Owned foreground CLI process for tests, not a product background manager."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class DaemonProcess:
    def __init__(self, project, comfy_root, *, port=0, log=None):
        self.project = Path(project)
        self.comfy_root = Path(comfy_root)
        self.port = port
        self.log = Path(log) if log else self.project / '.local/thin-cli-probes' / ('daemon-' + str(time.time_ns()) + '.log')
        self.child = None
        self.output = None
        self.ready = False
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('PYTHONPATH', None)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def start(self, *, timeout=120):
        with socket.socket() as probe:
            if self.port:
                if probe.connect_ex(('127.0.0.1', self.port)) == 0:
                    raise RuntimeError('Requested test port is occupied; no existing service was changed')
            else:
                probe.bind(('127.0.0.1', 0))
                self.port = probe.getsockname()[1]
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.output = self.log.open('ab')
        self.command = [sys.executable, '-B', '-m', 'comani', 'daemon', '--comfy-root', str(self.comfy_root),
                        '--host', '127.0.0.1', '--port', str(self.port), '--cpu', '--no-custom-nodes']
        try:
            self.child = subprocess.Popen(self.command, cwd=self.project, env=self.env, stdin=subprocess.DEVNULL,
                                          stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True)
            headers = {}
            if self.env.get('COMANI_TOKEN'):
                headers['Authorization'] = 'Bearer ' + self.env['COMANI_TOKEN']
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.child.poll() is not None:
                    raise RuntimeError(f'CLI exited {self.child.returncode}; inspect {self.log}')
                try:
                    request = urllib.request.Request(self.url + '/queue', headers=headers)
                    with opener.open(request, timeout=min(.5, max(.001, deadline - time.monotonic()))) as response:
                        body = json.load(response)
                    if 'queue_running' in body and 'queue_pending' in body:
                        self.ready = True
                        return self
                except (urllib.error.URLError, TimeoutError, ValueError):
                    pass
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
            raise TimeoutError(f'Test fixture readiness deadline {timeout:g}s expired; inspect {self.log}')
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        if self.output is not None:
            self.output.close()
            self.output = None


def listening(port):
    with socket.socket() as probe:
        return probe.connect_ex(('127.0.0.1', port)) == 0
