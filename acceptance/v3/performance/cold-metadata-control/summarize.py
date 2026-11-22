"""Calculate nearest-rank endpoints and retain the components of each tail observation."""
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from math import ceil
from pathlib import Path
from statistics import median

folder=Path(__file__).resolve().parent
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def groups(v):
    return {n:round(t,4) for n,t in [('p50',nearest(v,.50)),('p95',nearest(v,.95)),('max',max(v))]}|{'n':len(v)}

def nearest(v,p):
    return sorted(v)[ceil(len(v)*p)-1]

def item(r):
    m={k:r[k] for k in ('total_ms','before_enter_ms','hook_and_tool_prep_ms','worker_tool_build_envelope_ms')}
    if 'initial_coordinator_pass_to_first_yield_ms' in r:m['initial_coordinator_pass_ms']=r['initial_coordinator_pass_to_first_yield_ms']
    for field in ('main','supplement'):
        for phase in r[field]:
            k=phase['phase']
            if k in ('initialize','initialized','config/read','model/list','account/read','AppServerClient.start'):
                key=field+'.'+k
                m[key]=m.get(key,0)+phase['duration_ms']
    m['total_other_than_initialize_ms']=m['total_ms']-m['main.initialize']
    m['initialize_share_percent']=100*m['main.initialize']/m['total_ms']
    return m

def summarize(path):
    raw=json.loads(path.read_bytes())
    arms=defaultdict(list)
    for t in raw['results']:
        for row in t['rows']:
            arms[t['arm']].append({'sequence':t['sequence'],'position':row['position'],**item(row)})
    arm_stats={};paired={}
    for arm,rows in arms.items():
        metrics=[k for k in rows[0] if k not in ('sequence','position')]
        by={}
        for key,rs in [('all',rows),('first_in_process',[r for r in rows if r['position']==0]),('second_in_process',[r for r in rows if r['position']==1])]:
            ranked=sorted(rs,key=lambda r:r['total_ms'])
            by[key]={'metrics_ms':{k:groups([r[k] for r in rs]) for k in metrics},
                     'sample_at_p95_total':ranked[ceil(.95*len(ranked))-1]}
        arm_stats[arm]=by
    for pos in range(2):
        b={r['sequence']//2:r for r in arms['baseline'] if r['position']==pos}
        c={r['sequence']//2:r for r in arms['candidate'] if r['position']==pos}
        assert set(b)==set(c)
        paired[pos]={k:{'median_candidate_minus_baseline_ms':round(median(c[i][k]-b[i][k] for i in b),4),
            'candidate_slower_pairs':sum(c[i][k]>b[i][k] for i in b),'paired_count':len(b)}
            for k in ['total_ms','main.initialize','before_enter_ms','hook_and_tool_prep_ms','total_other_than_initialize_ms']}
    source=folder.parent/'p1/commands.jsonl'
    commands=[r for r in map(json.loads,source.read_text().splitlines())]
    starts={r['label']:r['at'] for r in commands if 'command' in r}
    ends={r['label']:r['ended'] for r in commands if 'exit_code' in r}
    intervals=[(datetime.fromisoformat(t),datetime.fromisoformat(ends.get('drive-'+k.split('-')[1],raw['ended'])))
               for k,t in starts.items() if k.startswith('start-') and k.endswith('-west')]
    overlap=[r['sequence'] for r in raw['results'] if any(max(datetime.fromisoformat(r['started']),s)<min(datetime.fromisoformat(r['ended']),e) for s,e in intervals)]
    return {'source':path.name,'sha256':sha(path),'start':raw['started'],'end':raw['ended'],'samples':len(raw['results'])*2,
            'total_metered_native_turns':sum(t['counts']['p0_meter_native_turns'] for t in raw['results']),
            'overlapping_paid_windows':overlap,'arms':arm_stats,'adjacent_same_position_pairs':paired,
            'observed_official_established_peers':sorted({(p['address'],p['port']) for t in raw['results'] for p in t.get('observed_native_established_peers',[])})}
if __name__=='__main__':
    report={k:summarize(folder/n) for k,n in [('loopback','samples.json'),('oauth','oauth-samples.json')] if (folder/n).is_file() and len((src:=json.loads((folder/n).read_bytes()))['results'])==len(src['order'])}
    dest=folder/'summary.json';dest.write_text(json.dumps(report,indent=2)+'\n')
    for k,arm in report.items():
        print(k,arm['samples'],arm['total_metered_native_turns'],arm['overlapping_paid_windows'],arm['observed_official_established_peers'])
        for name,versions in arm['arms'].items():
            group=versions['all'];metrics=group['metrics_ms']
            print(name,{x:metrics[x]['p95'] for x in metrics},'tail',{k:round(v,2) for k,v in group['sample_at_p95_total'].items()})
