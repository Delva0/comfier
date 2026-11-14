"""Run foreground CLI -> real basic example -> owned SIGTERM in an existing env."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _daemon_fixture import DaemonProcess, listening


def main(args):
    project = Path(__file__).resolve().parents[2]
    daemon = DaemonProcess(project, args.comfy_root, port=args.port)
    report = {'scope': 'thin CLI and basic SDK example in the current configured environment', 'steps': []}
    try:
        started = time.monotonic()
        daemon.start(timeout=args.startup_timeout)
        report['steps'].append({'command': daemon.command, 'ready': True, 'pid': daemon.child.pid,
                                'seconds': time.monotonic() - started})
        command = [sys.executable, '-B', str(project / 'examples/basic.py'), '--url', daemon.url]
        result = subprocess.run(command, cwd=project, env=daemon.env, text=True, capture_output=True, timeout=45)
        report['steps'].append({'command': command, 'returncode': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr})
        assert result.returncode == 0, result.stderr
    finally:
        daemon.stop()
        if daemon.child is not None:
            report['steps'].append({'action': 'terminate only the owned foreground CLI process',
                                    'pid': daemon.child.pid, 'returncode': daemon.child.returncode})
        report['passed'] = (len(report['steps']) == 3 and daemon.ready and
                            report['steps'][1]['returncode'] == 0 and daemon.child.poll() is not None and
                            daemon.child.returncode != -9 and not listening(daemon.port))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, default=Path(os.environ.get('COMANI_COMFY_ROOT', project.parent / 'ComfyUI')))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--startup-timeout', type=float, default=120)
    parser.add_argument('--output', type=Path, default=project / '.local/thin-cli-probes/readme-quickstart.json')
    raise SystemExit(main(parser.parse_args()))
