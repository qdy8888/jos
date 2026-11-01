"""Integration runner. Adapter module is explicit and must be trusted local code."""
import argparse
import importlib
import signal
import time
from .scheduler import Scheduler, Limits, MiB
from .resources import snapshot

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--db',required=True)
    p.add_argument('--adapter',required=True,help='trusted module:factory, implements reconcile and ensure_started')
    p.add_argument('--max-running',type=int,default=1)
    p.add_argument('--task-mib',type=int,default=768)
    p.add_argument('--reserve-mib',type=int,default=1536)
    a=p.parse_args()
    module,factory=a.adapter.split(':',1)
    adapter=getattr(importlib.import_module(module),factory)()
    s=Scheduler(a.db,adapter,Limits(task_bytes=a.task_mib*MiB,reserve_bytes=a.reserve_mib*MiB,max_running=a.max_running))
    stop=False
    def request_stop(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,request_stop)
    signal.signal(signal.SIGINT,request_stop)
    try:
        while not stop:
            s.tick(snapshot())
            time.sleep(1)
    finally:
        # Closing dispatcher does not signal external workers.
        s.close()

if __name__=='__main__': main()
