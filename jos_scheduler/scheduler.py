from dataclasses import dataclass
import fcntl
import json
import sqlite3
import time

MiB = 1024**2

@dataclass(frozen=True)
class Resources:
    # Minimum host MemAvailable and all applicable cgroup ancestor headrooms.
    headroom: int
    healthy: bool = True

@dataclass(frozen=True)
class Limits:
    task_bytes: int = 768 * MiB  # provisional reservation, NOT a measured task peak
    reserve_bytes: int = 1536 * MiB
    max_running: int = 1  # commissioning ceiling until measured
    batch_size: int = 1
    cooldown: float = 5.0
    recovery_samples: int = 3

class Scheduler:
    """Single dispatcher with durable intents and per-device active uniqueness.

    adapter.reconcile(task) -> absent/running/done/blocked
    adapter.ensure_started(task): idempotent by task['id']; never buy blindly.
    absent means VERIFIED no live worker AND side effects reconciled/resumable.
    No timeout ever implies absent. No worker is killed by this module.
    """
    def __init__(self, path, adapter, limits=Limits(), clock=time.monotonic):
        if min(limits.task_bytes, limits.max_running, limits.batch_size,
               limits.recovery_samples) <= 0 or limits.reserve_bytes < 0 or limits.cooldown < 0:
            raise ValueError('invalid limits')
        self.lock = open(str(path) + '.dispatcher.lock', 'a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, device TEXT NOT NULL, context TEXT NOT NULL,
          state TEXT NOT NULL, error TEXT, created REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS active_device ON tasks(device)
          WHERE state != 'done';
        ''')
        self.adapter, self.limits, self.clock = adapter, limits, clock
        self.good = 0
        self.last_start = float('-inf')
        self.reason = 'warming_up'

    def close(self):
        self.db.close()
        self.lock.close()

    def submit(self, task_id, device, context):
        # Existing ID must refer to exactly the same request. No order edits.
        encoded = json.dumps(context, sort_keys=True)
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row:
            if row['device'] != device or row['context'] != encoded:
                raise ValueError('idempotency key reused with different request')
            return task_id
        try:
            self.db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)',
                            (task_id, device, encoded, 'queued', None, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError('device already has unfinished task') from None
        return task_id

    def tasks(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM tasks ORDER BY created,id')]

    def _state(self, task, state, error=None):
        self.db.execute('UPDATE tasks SET state=?,error=? WHERE id=?',
                        (state, error, task['id']))

    def tick(self, resources):
        # Reconcile every unfinished intent, including queued tasks after an
        # integration restart. The external system is authoritative.
        reconcile_error = False
        for task in self.tasks():
            if task['state'] == 'done':
                continue
            try:
                status = self.adapter.reconcile(task)
                state = {'absent':'queued', 'running':'running', 'done':'done',
                         'blocked':'paused'}[status]
                self._state(task, state)
            except Exception as exc:
                self._state(task, 'paused', type(exc).__name__)
                reconcile_error = True
        if reconcile_error:
            self.good = 0
            self.reason = 'reconciliation_unavailable'
            return 0
        active = sum(t['state'] in ('running', 'starting', 'paused') for t in self.tasks())
        l = self.limits
        if not resources.healthy or resources.headroom < l.reserve_bytes + l.task_bytes:
            self.good = 0
            self.reason = 'memory_or_monitor_pause'
            return 0
        self.good += 1
        if self.good < l.recovery_samples:
            self.reason = 'warming_up'
            return 0
        if self.clock() - self.last_start < l.cooldown:
            self.reason = 'cooldown'
            return 0
        # Headroom already excludes live memory. Reserve another full task for
        # each active/uncertain worker to cover growth beyond current usage.
        slots = min(l.max_running - active,
                    (resources.headroom - l.reserve_bytes) // l.task_bytes - active,
                    l.batch_size)
        started = 0
        for task in self.tasks():
            if started >= max(0, slots):
                break
            if task['state'] != 'queued':
                continue
            self._state(task, 'starting')  # durable before external side effect
            try:
                self.adapter.ensure_started(task)
                self._state(task, 'running')
            except Exception as exc:
                # Could have started remotely: leave uncertain, never re-launch
                # without authoritative reconciliation on a later tick.
                self._state(task, 'paused', type(exc).__name__)
                self.good = 0
                self.reason = 'start_exception'
                return started
            started += 1
            self.last_start = self.clock()
        self.reason = 'admitted' if started else 'capacity_or_queue'
        return started
