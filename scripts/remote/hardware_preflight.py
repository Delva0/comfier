"""Record actual container resources before downloading model files."""
import json
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from disk_budget import OUTPUT_RESERVE, environment_increment, remaining_model_bytes

root = Path('/workspace/comani-acceptance')
root.mkdir(parents=True, exist_ok=True)
memory = {}
for line in Path('/proc/meminfo').read_text().splitlines():
    name, value = line.split(':', 1)
    if name in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
        memory[name + '_bytes'] = int(value.split()[0]) * 1024
limits = {}
for name in ['memory.max', 'memory.high', 'memory.current', 'memory.swap.max']:
    path = Path('/sys/fs/cgroup') / name
    if path.is_file():
        limits[name] = path.read_text().strip()
usage = shutil.disk_usage(root)
gpu = subprocess.check_output([
    'nvidia-smi', '--query-gpu=name,memory.total,driver_version,pci.bus_id',
    '--format=csv,noheader,nounits',
], text=True).strip()
assert '4090' in gpu
manifest = json.loads(Path(__file__).with_name('models.json').read_text())
remaining = remaining_model_bytes(manifest, root / 'ComfyUI/models')
required = remaining + environment_increment(root / 'next') + OUTPUT_RESERVE
assert usage.free >= required, f'Actual free disk {usage.free} below plan {required}'
report = {'at_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
          'python': platform.python_version(), 'gpu_csv': gpu,
          'disk_bytes': dict(zip(['total', 'used', 'free'], usage)),
          'proc_meminfo': memory, 'cgroup_limits': limits,
          'planned_min_free_disk_bytes': required}
(root / 'hardware.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2), flush=True)
