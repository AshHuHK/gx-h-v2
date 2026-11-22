"""Observe local load, storage and durable GX progress once a minute during a real acquisition."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from audit_workload import pulse
from run_workload import append


def read_if_present(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError,PermissionError):
        return None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--private",type=Path,required=True)
    p.add_argument("--evidence",type=Path,required=True)
    args=p.parse_args()
    while not args.private.is_dir():
        time.sleep(1)
    args.evidence.mkdir(parents=True,exist_ok=True)
    output=args.evidence/"progress-load.jsonl"
    while True:
        now=datetime.now(timezone.utc).isoformat()
        state=os.statvfs(args.private)
        mem=dict((k.strip(),v.strip()) for k,v in (line.split(":",1)
                    for line in Path("/proc/meminfo").read_text().splitlines()))
        record={"at":now,"system_load_1_5_15":os.getloadavg(),"mem_available":mem.get("MemAvailable"),
                "storage_available_bytes":state.f_bavail*state.f_frsize,"storage_block_size":state.f_frsize,
                "cgroup_cpu_max":read_if_present("/sys/fs/cgroup/cpu.max"),"cgroup_memory_max":read_if_present("/sys/fs/cgroup/memory.max"),
                "cgroup_cpu_stat":read_if_present("/sys/fs/cgroup/cpu.stat")}
        try:
            record["gx"]=pulse(args.private)
        except (FileNotFoundError,sqlite3.Error) as exc:
            record["gx_read_error"]=type(exc).__name__
        append(output,record)
        if (args.evidence/"finished.json").is_file():
            return
        time.sleep(60)


if __name__=="__main__":
    main()
