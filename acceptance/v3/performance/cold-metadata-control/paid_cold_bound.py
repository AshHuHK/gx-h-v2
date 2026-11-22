from pathlib import Path
import sys,json
from math import ceil
from collections import defaultdict
perf=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(perf))
from dispatch_phases import collect
from audit_workload import quantile
import argparse,hashlib
parser=argparse.ArgumentParser()
parser.add_argument('--paid-waves',required=True,type=int)
parser.add_argument('--private',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
a=parser.parse_args()
birth_path=perf/'p1/cold-process-births.jsonl'
births=[r for r in map(json.loads,birth_path.open()) if r['wave'] < a.paid_waves and r['all_expected_processes']]
loc=a.private
wave=[r['wave'] for r in births]
records={}
for arm,w in [('baseline','v21-gate0-xhigh'),('candidate','p1-2476f2b-xhigh')]:
 raw,err,_=collect(loc/w,wave); assert not err,err
 records[arm]=[r for r in raw if 'first_same_task_writer' in r]
b={(r['wave'],r['lane']):r for r in records['baseline']}
samples=[]
for w in births:
 for child in w['official_codex_processes']:
  t=child['estimated_birth_ns']; upper=t+child['tick_quantization_ns']+child['clock_sample_width_ns']
  rr=[r for r in records['candidate'] if r['wave']==w['wave'] and r['started_ns']<=upper and t<=r['first_same_task_writer']['started_ns']]
  assert len(rr)==1; r=rr[0];ref=b[r['wave'],r['lane']];e=r['first_same_task_writer']['started_ns']
  bc=ref['subphases_ms']['cold_dispatch_start_to_first_writer_ms'];ct=r['subphases_ms']['cold_dispatch_start_to_first_writer_ms'];lower=(e-upper)/1e6
  samples.append({'wave':r['wave'],'lane':r['lane'],'base_total_ms':bc,'cand_total_ms':ct,'cand_before_birth_upper_ms':(upper-r['started_ns'])/1e6,'cand_after_birth_lower_ms':lower,'candidate_post_birth_lower_minus_baseline_total_ms':lower-bc})
summaries={}
print('waves',wave,'n',len(samples))
for k in samples[0]:
 if k in ('wave','lane'):continue
 summaries[k]=quantile(r[k] for r in samples)
 print(k,summaries[k])
positive=sum(r['candidate_post_birth_lower_minus_baseline_total_ms']>0 for r in samples)
print('positive per matched Run',positive)
a.output.write_text(json.dumps({'wave_indices':wave,'count':len(samples),'pairs_with_positive_lower_bound':positive,'statistics':summaries,'samples':samples,'births_sha256':hashlib.sha256(birth_path.read_bytes()).hexdigest(),'basis':'For each baseline Run, post-birth time through first same-task GX writer cannot exceed its full dispatch-to-writer interval. Candidate uses the latest admissible Linux process-birth time. Match on scheduled arrival and lane. These bounds locate elapsed time after official child creation; they do not discriminate individual native metadata calls.'},indent=2)+'\n')

