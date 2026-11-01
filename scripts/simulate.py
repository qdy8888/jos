#!/usr/bin/env python3
"""Deterministic logical memory simulation, not real workload benchmarking."""
import json
import tempfile
from pathlib import Path
from jos_scheduler import Scheduler, Limits, Resources
from jos_scheduler.scheduler import MiB

class Model:
    def __init__(self):
        self.status={}; self.age={}; self.starts={}; self.disconnected=set()
    def reconcile(self,t):
        return 'blocked' if t['id'] in self.disconnected else self.status.get(t['id'],'absent')
    def ensure_started(self,t):
        self.starts[t['id']]=self.starts.get(t['id'],0)+1
        self.status[t['id']]='running'; self.age[t['id']]=0

results=[]
for count in (10,30,50,100):
    with tempfile.TemporaryDirectory() as tmp:
        m=Model(); s=Scheduler(Path(tmp)/'queue.db',m,Limits(max_running=8,cooldown=0,recovery_samples=3))
        for i in range(count): s.submit(str(i),f'device-{i}',{'order':f'preserved-{i}'})
        peak=0; max_queue=0; min_free=8192; pauses=0; injected=False; recovered=False
        for tick in range(2000):
            live=[k for k,v in m.status.items() if v=='running']
            # Logical 8GiB host, 2GiB other services, 512MiB per active task.
            free=(8192-2048-512*len(live))*MiB
            if 12 <= tick < 18: free=256*MiB; pauses+=1
            if tick==8 and live:
                m.disconnected.add(live[0])
            if tick==11: m.disconnected.clear()
            if tick==22 and live and not injected:
                # VERIFIED exited worker; same task/order is resumable.
                m.status[live[0]]='absent'; injected=True
            s.tick(Resources(free))
            if tick==24:
                s.close(); s=Scheduler(Path(tmp)/'queue.db',m,Limits(max_running=8,cooldown=0,recovery_samples=3)); recovered=True
            rows=s.tasks(); max_queue=max(max_queue,sum(t['state']=='queued' for t in rows))
            live=[k for k,v in m.status.items() if v=='running']; peak=max(peak,len(live))
            min_free=min(min_free,8192-2048-512*len(live))
            assert 8192-2048-512*len(live) >= 1536
            for k in live:
                m.age[k]+=1
                if m.age[k]>=10 and k not in m.disconnected: m.status[k]='done'
            if all(t['state']=='done' for t in rows): break
        assert all(t['state']=='done' for t in s.tasks())
        assert sum(m.starts.values()) == count + int(injected)
        results.append(dict(devices=count,completed=count,peak_running=peak,max_queued=max_queue,
                            modeled_min_free_MiB=min_free,pressure_ticks=pauses,
                            verified_exit_retries=int(injected),injected_pressure_free_MiB=256,dispatcher_restart=recovered,ticks=tick+1))
        s.close()
print(json.dumps({'kind':'logical simulation, not measured production memory','results':results},indent=2))
