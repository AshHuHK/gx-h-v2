"""Observe the same warm file read and durable scratch write at the start of each local control trial."""
import argparse
import json
import os
from pathlib import Path
import time

from run_workload import append,sha,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--private',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--trials',type=int,default=24)
    a=p.parse_args();expected=sha(a.reference);seen=set()
    plan=a.evidence/'control.json';planned=len(json.loads(plan.read_bytes())['order']) if plan.is_file() else a.trials
    while len(seen)<planned:
        for trial in sorted(a.private.glob('[0-9][0-9]-*')):
            if not trial.is_dir() or trial.name in seen:
                continue
            # Keep a fresh four-KiB write per measurement in the private control folder.
            data=[]
            for i in range(4):
                begin=time.perf_counter_ns();a.reference.read_bytes();read=(time.perf_counter_ns()-begin)/1e6
                begin=time.perf_counter_ns()
                with (trial/f'ambient-probe-{i}').open('xb') as out:
                    out.write(bytes(4096));out.flush();os.fsync(out.fileno())
                write=(time.perf_counter_ns()-begin)/1e6;data.append({'warm_read_ms':read,'fresh_create_write_fsync_close_ms':write})
            append(a.evidence/'ambient-disk.jsonl',{'trial':trial.name,'sequence':int(trial.name[:2]),'arm':trial.name.split('-',1)[1],
                'observed':utc(),'reference_sha256':expected,'reference_bytes':a.reference.stat().st_size,'new_file_bytes':4096,'samples':data})
            seen.add(trial.name)
        if len(seen)<planned:time.sleep(.5)


if __name__=='__main__':main()
