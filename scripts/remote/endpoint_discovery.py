"""Read one explicitly labelled tunnel endpoint for one exact boot run."""
import json
from urllib.parse import urlsplit


def discover(logs, role, run_id):
    key = 'COMANI_' + role.upper() + '_URL'
    for line in reversed(logs.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('run_id') != run_id or key not in record:
            continue
        url = record[key]
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname and parsed.hostname.endswith('.trycloudflare.com'):
            return url
    raise RuntimeError(f'No {role} endpoint labelled for boot run {run_id}')
