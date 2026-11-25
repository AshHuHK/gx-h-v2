"""Run the published, matched V6 interventions in separate GX stores and native homes."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import signal
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from fixture import PRODUCT, V6, digest, fixture_objects, prepare, read, save, seed_decision, setup_source

sys.path.insert(0, str(PRODUCT / 'gx-h'))
sys.path.insert(0, str(V6))
from gx_core.native_release import native_state
from gx_core.persistence import store
import live


def invoke(env, args, timeout):
    argv = [str(PRODUCT / 'bin/gx-p0'), *map(str, args)]
    begin = time.time_ns()
    with subprocess.Popen(argv, cwd=env['HOME'], env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
            raise RuntimeError(f'GX {args[0]} exceeded its driver deadline')
        if child.returncode not in (0, 75):
            # No authentication commands or tokens are passed to the product CLI.
            raise RuntimeError(f'GX {args[0]} exited {child.returncode}: {err[-500:]}')
        return {'argv':argv,'exit_code':child.returncode,'pid':child.pid,'started_ns':begin,
                'finished_ns':time.time_ns(),'stdout':json.loads(out)}


def bootstrap_fixture(db, state, run_id, initial):
    """Exercise the real decision service behind a clearly labelled, unpaid setup callback."""
    from gx_core.p0.roots import Roots
    from gx_core.p0 import v6_decisions
    row = db.execute('SELECT inputs_json FROM runs WHERE run_id=?', (run_id,)).fetchone()
    refs = {item['role']:item['artifact_ref'] for item in json.loads(row[0])['files']}
    task_id, = db.execute('SELECT task_id FROM v6_decision_tasks WHERE run_id=?', (run_id,)).fetchone()
    roots = Roots(db, run_id=run_id, cwd=state)
    root = roots._claim(task_id)
    tid, turn, call = ('fixture-'+kind+'-'+run_id for kind in ('thread','turn','call'))
    roots._bind(root, {'id':tid,'sessionId':tid,'cwd':root['cwd'],'source':'appServer'})
    # Public controls start with a synthetic logical head. Seed the acknowledged fixture input
    # only, with no provider reservation. Its deterministic IDs exclude it from live scoring.
    with store.transaction(db, transaction_owner='AcceptanceV6',operation='fixture_delivery',run_id=run_id):
        db.execute('INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,'
                   'interrupt,state,method,turn_id) VALUES (?,?,?,?,?,0,0,?,?,?)',
                   (run_id,'fixture-initial',tid,'fixture-initial',hashlib.sha256(root['brief'].encode()).hexdigest(),
                    'acknowledged','turn/start',turn))
    return v6_decisions.adopt(db,run_id=run_id,task_id=task_id,lease_id=root['lease_id'],thread_id=tid,
                              turn_id=turn,call_id=call,decision=seed_decision(refs,initial))


def inject(db, run_id, packet):
    from gx_core.artifacts.service import ArtifactService
    from gx_core.p0 import v6_goal
    alias = {}
    with store.transaction(db, transaction_owner='AcceptanceV6', operation='inject_observations',run_id=run_id):
        for i, obs in enumerate(packet['observations']):
            measurement = obs['measurement']
            artifact = ArtifactService(db).put(run_id=run_id,kind='synthetic_observation',media_type='application/json',
                content=json.dumps(measurement,ensure_ascii=False,allow_nan=False), origin='HOST_PUT',put_by='control-evaluator')
            v6_goal.record_observation(db,run_id,artifact.artifact_ref,source='Synthetic contrast, observation '+str(i+1),
                                       observation=measurement)
            alias[artifact.artifact_ref] = obs['id']
    return alias


def seed_world(world, review, env):
    from gx_core.p0 import v6_goal, v6_jobs
    state = world/'state'
    goal, initial, _ = fixture_objects()
    config = world/'goal-config.json'
    started = invoke({**env,'GX_CODEX_OFFLINE':'1'}, ['start','--state',state,'--run-key','matched-run',
                     '--goal',goal['question'],'--goal-config',config], 120)
    save(review/'start.json',started)
    run_id = started['stdout']['run_id']
    with native_state(state), closing(store.open_store(state/'gx.db')) as db:
        adopted = bootstrap_fixture(db,state,run_id,initial)
        v6_jobs.advance(db,run_id)
        jobs = [dict(row) for row in db.execute('SELECT job_id,capability_id,status FROM jobs WHERE run_id=?',(run_id,))]
        if len(jobs) != 1 or jobs[0]['capability_id'] != 'artifact-analysis':
            raise RuntimeError(f'Protected citation index was not admitted: {jobs}')
        labels = inject(db,run_id,read(review/'intervention.json'))
        v6_goal.ensure_task(db,run_id)
    live.capture(state/'gx.db', run_id, review/'before')
    result = {'run_id':run_id,'evidence_map':labels, 'unscored_scripted_seed':adopted,
              'protected_job':jobs[0],'setup_provider_calls':0,'pending_live_decision':True}
    save(review/'seed.json',result)
    return result


def latest_decisions(db, rid):
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute('SELECT decision_ref,thread_id,turn_id,call_id FROM v6_decisions '
             'WHERE run_id=? AND thread_id NOT LIKE ? ORDER BY rowid',(rid,'fixture-thread-%'))]


def execute_world(world, review, env, seconds):
    seed = read(review/'seed.json')
    rid = seed['run_id']
    state = world/'state'
    db = state/'gx.db'
    if latest_decisions(db,rid):
        raise ValueError('An experimental decision already exists; inspect the saved window before launching more work')
    # A single official decision is scored. Monitor the durable submission, stop the driver,
    # then reconcile accepted local Jobs without sending additional paid turns.
    args = [str(PRODUCT/'bin/gx-p0'),'run','--state',str(state),'--run-id',rid,'--timeout',str(seconds)]
    deadline = time.monotonic()+seconds
    with (review/'driver.stdout').open('x') as out, (review/'driver.stderr').open('x') as err:
        driver = subprocess.Popen(args,env=env,cwd=env['HOME'],stdout=out,stderr=err)
        try:
            decisions = []
            while time.monotonic()<deadline and driver.poll() is None:
                decisions = latest_decisions(db,rid)
                if decisions:
                    # Let the official host deliver its terminal/usage receipt before closing.
                    # A second decision never becomes evidence for the first window.
                    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as con:
                        done = con.execute('SELECT terminal_status FROM p0_root_turn_usage WHERE thread_id=? AND turn_id=?',
                                           (decisions[0]['thread_id'],decisions[0]['turn_id'])).fetchone()
                    if done and done[0]:
                        break
                time.sleep(.15)
        finally:
            if driver.poll() is None:
                driver.send_signal(signal.SIGINT)
            try:
                driver.wait(timeout=15)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait()
    decisions = latest_decisions(db,rid)
    if len(decisions) != 1:
        raise ValueError(f'Expected one accepted experimental decision; captured {len(decisions)}')
    decided = decisions[0]
    # GX still owns accepted Jobs and accounting while no new scientific turns are admitted.
    env_offline = {**env,'GX_CODEX_OFFLINE':'1'}
    settled = invoke(env_offline, ['run','--state',state,'--run-id',rid,'--timeout','12'], 45)
    save(review/'settlement-invocation.json', settled)
    live.capture(db, rid, review/'settled')
    resumed = invoke(env_offline,['resume','--state',state,'--run-id',rid,'--timeout','2'], 35)
    save(review/'resume-invocation.json',resumed)
    live.capture(db, rid, review/'reopened')
    sessions = list((Path(env['CODEX_HOME'])/'sessions').rglob('*'+decided['thread_id']+'*.jsonl'))
    if len(sessions)!=1:
        raise ValueError('Expected one original, native session rollout')
    packet = read(review/'intervention.json')
    window = {'before':'before','settled':'settled','reopened':'reopened','decision_ref':decided['decision_ref'],
              'rollout':str(sessions[0]),'resume_receipt':'resume-invocation.json','packet_id':packet['packet_id'],
              'evidence_map':seed['evidence_map']}
    save(review/'window.json',window)
    return window


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=('prepare','seed','live'))
    p.add_argument('--private-dir',type=Path,required=True)
    p.add_argument('--review-dir',type=Path,required=True)
    p.add_argument('--api-calls',type=int,choices=(1,4),default=1,help='Prepare a fresh protocol with this per-run cap (first historical trial used 4)')
    p.add_argument('--slot', help='Operate on one opaque world from the saved evaluator plan')
    p.add_argument('--seconds',type=int,default=300,choices=range(1,3601),metavar='1..3600')
    args=p.parse_args()
    private, review=args.private_dir.resolve(),args.review_dir.resolve()
    if args.stage=='prepare':
        plan=prepare(private,review,api_calls=args.api_calls)
        print(json.dumps({'matched_worlds':len(plan),'review_dir':str(review)},ensure_ascii=False))
        return
    plan = read(review/'plan.json')
    if 'source' in plan and plan['source']['source_sha256'] != setup_source()['source_sha256']:
        raise RuntimeError('Frozen control implementation changed after preparing this trial')
    worlds = plan['worlds']
    if args.slot and args.slot not in {w['slot'] for w in worlds}:
        p.error('Unknown world slot')
    for entry in worlds:
        if args.slot and args.slot != entry['slot']:
            continue
        world=private/entry['slot']
        records=review/entry['slot']
        if args.stage=='seed':
            if (records/'seed.json').exists():
                continue
            seed_home=world/'seed-home'
            seed_home.mkdir(mode=0o700,exist_ok=True)
            profile=seed_home/'profile.json'
            save(profile,{'default':{'provider':'openai','model':live.MODEL,'effort':'xhigh','multi_agent':False}})
            env = {**os.environ,'HOME':str(seed_home),'GX_P0_PROFILE':str(profile),'GX_CODEX_OFFLINE':'1'}
            seed_world(world,records,env)
        else:
            if (records/'window.json').exists():
                continue
            if not (records/'seed.json').exists():
                raise ValueError('Complete and retain each matching seed receipt before live scoring')
            env=live.live_environment(world/'native')
            if not live.preflight(env,records):
                raise RuntimeError('Live provider preflight did not complete')
            execute_world(world,records,env,args.seconds)
    print(json.dumps({'stage':args.stage,'review_dir':str(review)}))


if __name__=='__main__':
    main()
