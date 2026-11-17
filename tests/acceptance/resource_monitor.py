"""Small disposable-daemon resource sampler used by independent acceptance."""


def start(directory):
    import json
    import threading
    import time
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stop_file = directory / 'monitor.stop'
    stop_file.unlink(missing_ok=True)

    def sample():
        import os
        import resource
        import subprocess

        import psutil
        import torch
        peaks = {'process_rss_bytes': 0, 'gpu_board_used_mib': 0, 'cgroup_current_bytes': 0}
        process = psutil.Process(os.getpid())
        with (directory / 'resource-samples.jsonl').open('w') as log:
            while not stop_file.exists():
                item = {'unix_time': time.time(), 'rss_bytes': process.memory_info().rss,
                        'torch_allocated_bytes': torch.cuda.memory_allocated(),
                        'torch_reserved_bytes': torch.cuda.memory_reserved()}
                try:
                    item['gpu_board_used_mib'] = int(subprocess.check_output([
                        'nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits',
                    ], text=True, timeout=5).strip())
                except Exception:
                    item['gpu_board_used_mib'] = 0
                group = Path('/sys/fs/cgroup/memory.current')
                item['cgroup_current_bytes'] = int(group.read_text()) if group.exists() else 0
                peaks['process_rss_bytes'] = max(peaks['process_rss_bytes'], item['rss_bytes'])
                peaks['gpu_board_used_mib'] = max(peaks['gpu_board_used_mib'], item['gpu_board_used_mib'])
                peaks['cgroup_current_bytes'] = max(peaks['cgroup_current_bytes'], item['cgroup_current_bytes'])
                peaks['kernel_process_rss_highwater_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                peaks['torch_max_allocated_bytes'] = torch.cuda.max_memory_allocated()
                peaks['torch_max_reserved_bytes'] = torch.cuda.max_memory_reserved()
                log.write(json.dumps(item) + '\n')
                log.flush()
                (directory / 'resource-peaks.json').write_text(json.dumps(peaks, indent=2))
                time.sleep(1)
    thread = threading.Thread(target=sample, daemon=True, name='comfier-acceptance-resource-sampler')
    thread.start()
    return {'sampling_started': True, 'interval_seconds': 1}


def stop(directory):
    import json
    import time
    from pathlib import Path
    directory = Path(directory)
    (directory / 'monitor.stop').touch()
    time.sleep(1.1)
    return json.loads((directory / 'resource-peaks.json').read_text())
