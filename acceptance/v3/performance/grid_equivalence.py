"""Compare all measured grid coordinates, evaluated thresholds and accepted scientific dispositions by Run key."""
import argparse
from contextlib import closing
import json
from pathlib import Path

from audit_workload import db_file,read_artifact
from run_workload import save,utc


def arm(work):
    output={}
    for file in sorted(work.glob('state-*/gx.db')):
        with closing(db_file(file)) as conn:
            state=file.parent
            def value(ref):
                art=conn.execute('SELECT * FROM artifacts WHERE artifact_ref=?',(ref,)).fetchone()
                return json.loads(read_artifact(state,art))
            for run in conn.execute('SELECT run_id,run_key FROM p0_workspace_runs'):
                rid=run['run_id'];routes={};products={};evidence=[]
                for record in conn.execute("SELECT artifact_ref FROM artifacts WHERE run_id=? AND kind='research_evidence'",(rid,)):
                    evidence.append(record['artifact_ref'])
                    for route in value(record['artifact_ref'])['routes']:
                        grid=value(route['input_ref']);observed=value(route['observations_ref']);score=value(route['evaluation_ref']);route_key=route['path_id']
                        if route_key in routes:raise ValueError('duplicate route in single-round comparison')
                        routes[route_key]={'status':route['status'],'outcome_kind':route['outcome_kind'],'method_id':observed['method_id'],
                            'method_version':observed['method_version'],'inputs':grid['candidates'],'points':observed['observations'],
                            'evaluation':{key:score[key] for key in ('observations','threshold','max_score','count_at_or_above')}}
                for s in conn.execute("SELECT s.submission_id,t.role,s.artifact_refs_json FROM submissions s JOIN tasks t USING(task_id) WHERE t.run_id=? AND s.result='ACCEPTED'",(rid,)):
                    refs=json.loads(s['artifact_refs_json']);body=value(refs[0]);role=s['role']
                    if role in products:raise ValueError('duplicate accepted scientific role in single-round comparison')
                    if role=='evidence_reviewer':
                        disposition={f['path_id']:f['supported'] for f in body['findings']}
                    else:
                        disposition={'stopped_with_reason':bool(body.get('stop_reason')),'continue_paths':body.get('continue_paths',[]),
                            'next_grids':body.get('next_grids',{}),'pivots':body.get('pivots',[])}
                    products[role]={'submission_id':s['submission_id'],'product_artifact':refs[0],'disposition':disposition}
                output[run['run_key']]={'run_id':rid,'routes':routes,'accepted':products,'evidence_refs':evidence}
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--baseline',required=True,type=Path);p.add_argument('--candidate',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--expected-runs',type=int,default=26);a=p.parse_args()
    b=arm(a.baseline);c=arm(a.candidate);rows=[];issues=[]
    if len(b)!=a.expected_runs or set(b)!=set(c):issues.append('different or incomplete Run keys')
    for key in sorted(b.keys()&c.keys()):
        x,y=b[key],c[key];same=x['routes']==y['routes'];levels={k:{role:body['disposition'] for role,body in z['accepted'].items()} for k,z in [('baseline',x),('candidate',y)]}
        if not same:issues.append('different measured routes '+key)
        if set(levels['baseline'])!={'evidence_reviewer','planner'} or levels['baseline']!=levels['candidate']:issues.append('different accepted scientific dispositions '+key)
        rows.append({'run_key':key,'baseline_run_id':x['run_id'],'candidate_run_id':y['run_id'],'same_inputs_measurements_and_evaluations':same,
            'baseline_routes':x['routes'],'candidate_routes':y['routes'],'accepted':{'baseline':x['accepted'],'candidate':y['accepted']},
            'same_accepted_dispositions':levels['baseline']==levels['candidate'],'evidence_refs':{'baseline':x['evidence_refs'],'candidate':y['evidence_refs']}})
    result={'captured':utc(),'matched_runs':len(rows),'matched_routes':sum(len(r['baseline_routes']) for r in rows),
        'matched_grid_points':sum(len(route['points']) for r in rows for route in r['baseline_routes'].values()),'rows':rows,'issues':issues,
        'scope':'exact original inputs, numerical observation rows and frozen score summaries; accepted route-level support and planner stop/continue/pivot dispositions. Model-written explanations remain in their respective original artifact references.',
        'result':'PASS' if not issues else 'REVIEW'};save(a.output,result);print(json.dumps({k:result[k] for k in ('matched_runs','matched_routes','matched_grid_points','issues','result')},indent=2));return bool(issues)


if __name__=='__main__':raise SystemExit(main())
