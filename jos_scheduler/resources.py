"""Linux cgroup v2 admission snapshot. Unknown readings fail closed."""
from pathlib import Path
from .scheduler import Resources

def snapshot(proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup')):
    try:
        mem = dict(line.split(':', 1) for line in (proc/'meminfo').read_text().splitlines())
        available = int(mem['MemAvailable'].split()[0]) * 1024
        lines = (proc/'self/cgroup').read_text().splitlines()
        relative = next(line[3:] for line in lines if line.startswith('0::'))
        node = cgroup / relative.lstrip('/')
        headrooms = [available]
        while True:
            limit_file = node/'memory.max'
            if limit_file.exists():
                for name in ('memory.max', 'memory.high'):
                    value = (node/name).read_text().strip()
                    if value != 'max':
                        headrooms.append(max(0, int(value) - int((node/'memory.current').read_text())))
            elif node != cgroup:
                raise ValueError('cgroup unavailable')
            if node == cgroup:
                break
            node = node.parent
            if not node.is_relative_to(cgroup):
                raise ValueError('invalid cgroup path')
        return Resources(min(headrooms))
    except (OSError, ValueError, KeyError, StopIteration):
        return Resources(0, False)
