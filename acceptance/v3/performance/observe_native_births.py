"""Observe Linux birth times of the pinned Codex processes belonging to future benchmark arrivals."""
import argparse
import json
import os
from pathlib import Path
import time

import psutil

from run_workload import OFFICIAL_BINARY_SHA256,append,sha,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--official',type=Path,required=True);p.add_argument('--first-wave',type=int,required=True)
    a=p.parse_args(); binary=a.official.resolve(); rate=os.sysconf('SC_CLK_TCK')
    if sha(binary)!=OFFICIAL_BINARY_SHA256:
        raise RuntimeError('official Codex executable digest mismatch')
    f=a.evidence/'cold-process-births.jsonl';arrivals=a.evidence/'arrivals.jsonl'
    for wave in range(a.first_wave,13):
        while True:
            known={row['wave']:row for row in map(json.loads,arrivals.read_text().splitlines())}
            if wave in known:
                driver=known[wave]['driver_pid'];break
            time.sleep(1)
        birth={};deadline=time.monotonic()+90
        while len(birth)<2 and time.monotonic()<deadline:
            try:
                children=psutil.Process(driver).children(recursive=True)
            except psutil.NoSuchProcess:
                break
            for proc in children:
                if proc.pid in birth:
                    continue
                try:
                    if Path(proc.exe()).resolve()!=binary:
                        continue
                    # The first field following the closing comm delimiter is field 3.
                    stat=Path(f'/proc/{proc.pid}/stat').read_text();tick=int(stat[stat.rfind(')')+2:].split()[19]);
                    before=time.time_ns();boottime=time.clock_gettime_ns(time.CLOCK_BOOTTIME);after=time.time_ns()
                    birth[proc.pid]={'pid':proc.pid,'driver_pid':driver,'birth_ticks':tick,'ticks_per_second':rate,
                        'estimated_birth_ns':(before+after)//2-boottime+tick*1_000_000_000//rate,
                        'clock_sample_width_ns':after-before,'tick_quantization_ns':1_000_000_000//rate,'observed_at':utc()}
                except (psutil.NoSuchProcess,psutil.AccessDenied,FileNotFoundError):
                    continue
            if len(birth)<2:time.sleep(.15)
        append(f,{'wave':wave,'recorded':utc(),'driver_pid':driver,'official_codex_processes':list(birth.values()),
            'method':'read-only /proc descendant start ticks with wall minus CLOCK_BOOTTIME conversion','all_expected_processes':len(birth)==2})


if __name__=='__main__':
    main()
