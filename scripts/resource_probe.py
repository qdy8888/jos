#!/usr/bin/env python3
"""Read-only Ubuntu 24.04 resource report. No commands, credentials, or orders dumped."""
import argparse
import json
import os
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('--service', required=True, help='Exact existing systemd unit')
p.add_argument('--task-pid-file', help='Optional JSON array of known registration worker PIDs')
a = p.parse_args()
if not a.service or a.service.startswith('-') or '/' in a.service:
    p.error('invalid service unit')
props = ['MemoryCurrent','MemoryPeak','MemoryMax','MemoryHigh','EffectiveMemoryMax',
         'ControlGroup','MainPID','TasksCurrent','ActiveState','LoadState','CPUUsageNSec']
r = subprocess.run(['systemctl','show',a.service,'--no-pager',
                    '--property=' + ','.join(props)], capture_output=True, text=True)
report = {'service': a.service, 'systemd': {}, 'errors': [],
          'registration_task_count': None,
          'task_count_note':'OS process count cannot identify business tasks; supply authoritative PID inventory'}
if r.returncode:
    report['errors'].append('systemctl show failed')
else:
    report['systemd'] = dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
report['meminfo'] = {k:v.strip() for k,v in
                     (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines())
                     if k in ('MemTotal','MemAvailable','SwapTotal','SwapFree')}
report['cgroup_ancestors'] = []
cg = report['systemd'].get('ControlGroup')
root = Path('/sys/fs/cgroup')
if cg:
    node = root / cg.lstrip('/')
    while node.is_relative_to(root):
        entry = {'path': str(node)}
        for name in ('memory.current','memory.peak','memory.max','memory.high','memory.events','memory.pressure','cgroup.procs'):
            try:
                entry[name] = (node/name).read_text().strip()
            except OSError:
                entry[name] = None
        report['cgroup_ancestors'].append(entry)
        if node == root:
            break
        node = node.parent
report['processes'] = []
# Include descendants in nested cgroups, omit command lines and environment.
pids = set()
if cg:
    for f in (root/cg.lstrip('/')).rglob('cgroup.procs'):
        try:
            pids.update(int(x) for x in f.read_text().split())
        except OSError:
            report['errors'].append('process inventory changed/unreadable')
for pid in sorted(pids):
    try:
        status = dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        cpu = subprocess.run(['ps','-p',str(pid),'-o','pcpu='], capture_output=True,text=True)
        report['processes'].append({'pid':pid, 'cpu_percent_lifetime_average':cpu.stdout.strip(), **{k:status.get(k,'').strip() for k in ('Name','VmRSS','VmHWM','Threads')}})
    except OSError:
        pass
if a.task_pid_file:
    supplied = json.loads(Path(a.task_pid_file).read_text())
    if not isinstance(supplied,list) or any(type(x) is not int or x <= 0 for x in supplied):
        p.error('PID inventory must be a JSON array of positive integers')
    report['known_worker_pids_alive'] = sorted(set(supplied) & pids)
    report['registration_task_count'] = len(report['known_worker_pids_alive'])
    report['task_count_note'] = 'Known live worker count only; queued/remote tasks require business database read-only adapter'
print(json.dumps(report, ensure_ascii=False, indent=2))
