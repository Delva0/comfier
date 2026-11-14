"""Independently compare every Python source, archive and installed package file."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

import comani

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', required=True, type=Path)
parser.add_argument('--sha256', required=True)
parser.add_argument('--report', required=True, type=Path)
args = parser.parse_args()
assert hashlib.sha256(args.wheel.read_bytes()).hexdigest() == args.sha256
root = Path(__file__).resolve().parents[2]
installed = Path(comani.__file__).parent
with zipfile.ZipFile(args.wheel) as wheel:
    sources = {name: hashlib.sha256(wheel.read(name)).hexdigest()
               for name in wheel.namelist() if name.startswith('comani/') and name.endswith('.py')}
for name, digest in sources.items():
    assert hashlib.sha256((root / 'src' / name).read_bytes()).hexdigest() == digest, name
    assert hashlib.sha256((installed.parent / name).read_bytes()).hexdigest() == digest, name
record = {'passed': True, 'wheel_sha256': args.sha256, 'installed': str(installed), 'files': sources}
args.report.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps({'passed': True, 'wheel_sha256': args.sha256, 'python_files_compared': len(sources),
                  'installed': str(installed)}))
