"""Time owner operations only inside a test CLI subprocess, without modifying either staged release."""
from contextvars import ContextVar
from functools import wraps
import json
import os
from pathlib import Path
import runpy
import sys
import time

entry = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(entry.parents[1] / 'gx-h'))
from gx_core.p0.roots import Roots
from gx_core.p0.root_delivery import RootDelivery

current_task = ContextVar('current_task', default=None)
stream = open(os.environ['GX_CONTROL_TIMINGS'], 'a', buffering=1)


def instrument(cls, name):
    original = getattr(cls, name)

    @wraps(original)
    def measured(self, *args, **kwargs):
        binding = current_task.set(args[0]) if name == 'dispatch' else None
        task = current_task.get()
        if not task and name != '__enter__':
            return original(self, *args, **kwargs)
        begin = time.time_ns()
        label = args[0] if name == '_request' else name
        outcome = 'returned'
        try:
            value = original(self, *args, **kwargs)
            if name == '_claim':
                outcome = 'none' if value is None else ('existing_lease' if value.get('thread_id') else 'new_root')
            return value
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            end = time.time_ns()
            root = self.roots if isinstance(self, RootDelivery) else self
            stream.write(json.dumps({'owner': cls.__name__, 'operation':label, 'run_id':root.run_id,
                'task_id':task, 'started_ns':begin,'ended_ns':end,'duration_ms':(end-begin)/1e6,
                'outcome':outcome},separators=(',',':'))+'\n')
            if binding is not None:
                current_task.reset(binding)
    setattr(cls, name, measured)


for operation in ('__enter__','dispatch','_claim','_materialize','_request','_resume','_goal_for_budget'):
    instrument(Roots, operation)
for operation in ('_reserve','_ack','deliver'):
    instrument(RootDelivery, operation)
runpy.run_path(str(entry),run_name='__main__')
