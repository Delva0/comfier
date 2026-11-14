"""Use official diagnostics without exposing account credentials."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[2]
state = json.loads((root / '.local/remote-acceptance/rental.json').read_text())
cli = root / '.local/vast-tools/bin/vastai'
for arguments in [
    ['logs', str(state['instance_id']), '--tail', '70'],
]:
    result = subprocess.run([str(cli), *arguments], capture_output=True, text=True, timeout=60)
    print(json.dumps({'operation': arguments[0], 'returncode': result.returncode}), flush=True)
    if arguments[0] == 'logs':
        saved = root / '.local/remote-acceptance' / ('provider-' + str(state['instance_id']) + '.log')
        saved.write_text(result.stdout)
        saved.chmod(0o600)
        token_file = root / '.local/remote-acceptance/service-token'
        safe = result.stdout.replace(token_file.read_text(), '[service-token]') if token_file.exists() else result.stdout
        print('\n'.join(line[:400] for line in safe[-12000:].splitlines() if len(line) < 1000))
