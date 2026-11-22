"""Read dispatch intervals in the 24 ordinary official-binary callback control trials."""
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import sys

here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent))
from audit_workload import quantile
from dispatch_phases import collect,compare,summary

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--private',type=Path,required=True)
    args=parser.parse_args()
    path=here.parent/'callback-control-2476f2b/control.json'
    original=json.loads(path.read_bytes())
    assert original['result']=='PASS' and len(original['trials'])==24
    samples=defaultdict(list)
    cold_rows=[]
    for trial in original['trials']:
        assert not trial['instrumented']
        folder=args.private/f"{trial['sequence']:02d}-{trial['arm']}"
        assert sha(folder/'metrics.jsonl')==trial['metrics_sha256']
        rows,issues,_=collect(folder,[0],True)
        assert not issues,issues
        for row in rows:
            samples[trial['arm']].append(row)
            if 'first_same_task_writer' in row:
                cold_rows.append({'sequence':trial['sequence'],'arm':trial['arm'],'lane':row['lane'],
                                  'cold_ms':row['subphases_ms']['cold_dispatch_start_to_first_writer_ms']})
    output={'control_sha256':sha(path),'trial_count':len(original['trials']),'receipt_join_issues':[],
            'metrics':{arm:summary(rows) for arm,rows in samples.items()},
            'operation_counts':{arm:dict(Counter(r['operation'] for r in rows)) for arm,rows in samples.items()},
            'standardization':compare(samples),'matched_cold_starts':cold_rows}
    (here/'control-dispatch.json').write_text(json.dumps(output,indent=2)+'\n')

if __name__=='__main__':
    main()
