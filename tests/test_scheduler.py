import tempfile
import unittest
from pathlib import Path
from jos_scheduler import Scheduler, Limits, Resources
from jos_scheduler.scheduler import MiB
from jos_scheduler.resources import snapshot

class Fake:
    def __init__(self):
        self.remote = {}; self.starts = {}; self.fail = False; self.offline = set()
    def reconcile(self, t):
        if t['id'] in self.offline:
            return 'blocked'
        return self.remote.get(t['id'], 'absent')
    def ensure_started(self, t):
        if self.remote.get(t['id']) != 'running':
            self.starts[t['id']] = self.starts.get(t['id'],0)+1
            self.remote[t['id']] = 'running'
        if self.fail:
            self.fail = False
            raise ConnectionError('ack lost')

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'q.db'
        self.a = Fake(); self.s = Scheduler(self.path,self.a,Limits(max_running=8,cooldown=0,recovery_samples=1))
    def tearDown(self):
        self.s.close(); self.tmp.cleanup()
    def test_duplicate_and_lock(self):
        self.s.submit('1','d',{'order':'existing'})
        self.s.submit('1','d',{'order':'existing'})
        with self.assertRaises(ValueError): self.s.submit('2','d',{})
        with self.assertRaises(ValueError): self.s.submit('1','d',{})
        with self.assertRaises(BlockingIOError): Scheduler(self.path,self.a)
    def test_ack_loss_and_restart(self):
        self.s.submit('1','d',{'order':'existing'}); self.a.fail=True
        self.s.tick(Resources(6*1024**3))
        self.assertEqual(self.s.tasks()[0]['state'],'paused')
        self.s.close(); self.s=Scheduler(self.path,self.a,Limits(cooldown=0,recovery_samples=1))
        self.s.tick(Resources(6*1024**3))
        self.assertEqual(self.a.starts,{'1':1})
        self.assertEqual(self.s.tasks()[0]['state'],'running')
    def test_pressure_does_not_kill(self):
        self.s.submit('1','d',{}); self.s.tick(Resources(6*1024**3))
        self.s.submit('2','e',{}); self.s.tick(Resources(0))
        self.assertEqual(self.a.remote['1'],'running'); self.assertNotIn('2',self.a.remote)
        self.s.tick(Resources(6*1024**3)); self.assertIn('2',self.a.remote)
    def test_disconnection_blocks_relaunch(self):
        self.s.submit('1','d',{}); self.s.tick(Resources(6*1024**3))
        self.a.offline.add('1'); self.s.tick(Resources(6*1024**3))
        self.assertEqual(self.s.tasks()[0]['state'],'paused')
        self.a.offline.clear(); self.s.tick(Resources(6*1024**3))
        self.assertEqual(self.a.starts['1'],1)
    def test_verified_process_exit_resumes_same_context(self):
        self.s.submit('1','d',{'order':'same'})
        self.s.tick(Resources(6*1024**3)); self.a.remote['1']='absent'
        self.s.tick(Resources(6*1024**3))
        self.assertEqual(self.s.tasks()[0]['context'],'{"order": "same"}')
        self.assertEqual(self.a.starts['1'],2)
    def test_monitor_failure(self):
        self.assertFalse(snapshot(Path('/nonexistent')).healthy)
    def test_hysteresis(self):
        self.s.close(); self.s=Scheduler(self.path,self.a,Limits(recovery_samples=3,cooldown=0))
        self.s.submit('1','d',{})
        self.assertEqual([self.s.tick(Resources(6*1024**3)) for _ in range(3)],[0,0,1])

if __name__ == '__main__': unittest.main()

class ResourceTests(unittest.TestCase):
    def test_ancestor_limit_and_high(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); proc=root/'proc'; cg=root/'cg'
            (proc/'self').mkdir(parents=True); (cg/'parent/child').mkdir(parents=True)
            (proc/'meminfo').write_text('MemAvailable: 8388608 kB\n')
            (proc/'self/cgroup').write_text('0::/parent/child\n')
            for path,maxval,high,current in [(cg/'parent','1073741824','max','536870912'),(cg/'parent/child','max','805306368','0')]:
                (path/'memory.max').write_text(maxval)
                (path/'memory.high').write_text(high)
                (path/'memory.current').write_text(current)
            self.assertEqual(snapshot(proc,cg).headroom,512*MiB)
            (cg/'parent/memory.current').unlink()
            self.assertFalse(snapshot(proc,cg).healthy)
