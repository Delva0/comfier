"""Label an actual cloudflared URL by boot run and purpose, without secrets."""
import argparse
import json
import re
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('role', choices=['control', 'daemon'])
parser.add_argument('--log', type=Path, required=True)
parser.add_argument('--run-id', required=True)
args = parser.parse_args()
for _ in range(180):
    text = args.log.read_text(errors='replace') if args.log.exists() else ''
    urls = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
    if urls:
        result = {'run_id': args.run_id, 'role': args.role,
                  'COMANI_' + args.role.upper() + '_URL': urls[-1]}
        (args.log.parent / ('endpoint-' + args.role + '.json')).write_text(json.dumps(result))
        print(json.dumps(result), flush=True)
        break
    time.sleep(1)
else:
    raise SystemExit('No actual tunnel URL after 180 seconds: ' + args.role)
