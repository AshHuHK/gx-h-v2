"""Sample actual established peers of official Codex descendants already running in isolated GX."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time

import psutil

from run_workload import OFFICIAL_BINARY_SHA256,save,sha,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',required=True,type=Path)
    p.add_argument('--official',required=True,type=Path);p.add_argument('--timeout',type=int,default=120)
    a=p.parse_args(); deadline=time.monotonic()+a.timeout;records=[]
    observed_binary=sha(a.official)
    if observed_binary!=OFFICIAL_BINARY_SHA256:
        raise RuntimeError('official Codex executable digest mismatch')
    while time.monotonic()<deadline:
        records=[]
        log=a.evidence/'arrivals.jsonl'
        arrivals=[json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []
        if arrivals:
            try:
                children=psutil.Process(arrivals[-1]['driver_pid']).children(recursive=True)
            except psutil.NoSuchProcess:
                children=[]
            for proc in children:
                try:
                    if Path(proc.exe()).resolve()!=a.official.resolve():
                        continue
                    peers=Counter((c.raddr.ip,c.raddr.port) for c in proc.connections(kind='inet')
                                  if c.status=='ESTABLISHED' and c.raddr)
                except (psutil.NoSuchProcess,psutil.AccessDenied):
                    continue
                records.append({'pid':proc.pid,'established_peers':[{'address':h,'port':port,'sockets':n} for (h,port),n in sorted(peers.items())]})
            unexpected=[c for r in records for c in r['established_peers'] if (c['address'],c['port']) not in [('127.0.0.1',7890),('127.0.0.1',7891)]]
            if unexpected or (len(records)>=2 and all(r['established_peers'] for r in records)):
                break
        time.sleep(1)
    observed=bool(len(records)>=2 and all(r['established_peers'] and all(c['address']=='127.0.0.1' and c['port'] in (7890,7891) for c in r['established_peers']) for r in records))
    save(a.evidence/'proxy-observation.json',{'at':utc(),'observation':'read-only Linux socket inspection of official Codex descendants of active isolated public GX driver',
        'observer_version':psutil.__version__,'official_binary_sha256':observed_binary,'official_app_servers':records,'all_observed_connections_to_clash':observed})
    return not observed


if __name__=='__main__':
    raise SystemExit(main())
