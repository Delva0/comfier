"""Prove a real >125-second remote wait polls one invocation without resubmit."""
import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from comfier import Client
from comfier.transport import HTTPTransport


def once(marker, seconds, directory=None):
    import json
    import socket
    import tempfile
    import time
    from pathlib import Path
    directory = Path(directory) if directory else Path(tempfile.gettempdir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('comfier-longwait-' + marker + '.jsonl')
    with path.open('a') as stream:
        stream.write(json.dumps({'started': time.time()}) + '\n')
    time.sleep(seconds)
    return {'executions': len(path.read_text().splitlines()), 'seconds': seconds,
            'host': socket.gethostname()}


class AuditedTransport(HTTPTransport):
    def __init__(self, url, token):
        super().__init__(url, token=token)
        self.submits = []
        self.waits = []

    async def request(self, method, **params):
        if method == 'submit':
            self.submits.append(params['call_id'])
        if method == 'wait_result':
            self.waits.append({'call_id': params['call_id'], 'wait_seconds': params.get('wait_seconds')})
            print(json.dumps({'poll': len(self.waits), **self.waits[-1]}), flush=True)
        return await super().request(method, **params)


async def main(args):
    transport = AuditedTransport(args.url, os.environ.get('COMFIER_TOKEN'))
    async with Client(transport=transport) as remote:
        assert remote.handshake.get('capabilities', {}).get('bounded_wait') is True
        started = time.monotonic()
        call = remote.python(once, inputs={'marker': uuid.uuid4().hex, 'seconds': args.seconds, 'directory': args.remote_directory}, return_call=True)
        value = await (await call.output()).fetch()
        elapsed = time.monotonic() - started
        assert value['executions'] == 1 and elapsed >= args.seconds
        assert transport.submits == [call.id]
        assert len(transport.waits) >= int(args.seconds // 20) + 1 and all(p['call_id'] == call.id for p in transport.waits)
        assert all(p['wait_seconds'] == 20.0 for p in transport.waits)
        result = {'passed': True, 'elapsed_seconds': elapsed, 'call_id': call.id,
                  'submit_count': 1, 'wait_query_count': len(transport.waits), 'remote': value}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--seconds', type=float, default=130)
    parser.add_argument('--remote-directory', help='Optional remote test-evidence directory; defaults to the remote temporary directory')
    asyncio.run(main(parser.parse_args()))
