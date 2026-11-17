"""Exercise a test-fixture deadline around the foreground thin CLI.

Historical dev_server results remain unchanged; this writes a separate local report.
"""
import argparse
import json
import os
from pathlib import Path

from _daemon_fixture import DaemonProcess, listening


def main(args):
    project = Path(__file__).resolve().parents[2]
    daemon = DaemonProcess(project, args.comfy_root, port=args.port)
    failure = None
    try:
        daemon.start(timeout=args.startup_timeout)
    except TimeoutError as error:
        failure = str(error)
    finally:
        daemon.stop()
    assert failure and daemon.child is not None and daemon.child.poll() is not None
    assert not daemon.ready and not listening(daemon.port)
    report = {'passed': True, 'scope': 'test-fixture readiness timeout, not a daemon background-management API',
              'recorded_pid': daemon.child.pid, 'start_returncode': daemon.child.returncode,
              'ready': daemon.ready, 'startup_error': failure, 'recorded_child_alive': False,
              'port_listening': False, 'log': str(daemon.log)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    project = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comfy-root', type=Path, default=Path(os.environ.get('COMFIER_COMFY_ROOT', project.parent / 'ComfyUI')))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--startup-timeout', type=float, default=.001)
    parser.add_argument('--output', type=Path, default=project / '.local/thin-cli-probes/launcher-timeout.json')
    main(parser.parse_args())
