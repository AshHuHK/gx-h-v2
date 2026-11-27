"""Deliver one bounded PI question to the scholarly branch of an ongoing yeast study."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def observe(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute('SELECT run_id,status FROM v6_goals').fetchone()
        if not run:
            return None
        modules = conn.execute("""SELECT m.module_id,m.scope,m.branch_id,r.thread_id,t.task_id
            FROM v62_modules m JOIN v6_decision_tasks d USING(run_id,scope)
            JOIN tasks t ON t.task_id=d.task_id JOIN p0_roots r ON r.task_id=t.task_id
            WHERE m.run_id=? AND m.status='OPEN' AND r.provider='openai'
              AND t.status IN ('CLAIMED','RUNNING') AND EXISTS (
                SELECT 1 FROM p0_root_deliveries rd WHERE rd.thread_id=r.thread_id
                AND rd.method='turn/start' AND rd.state='acknowledged')
            ORDER BY m.rowid""",(run['run_id'],)).fetchall()
        literature = next((r for r in modules if r['module_id']=='scholarly_investigation'), None)
        if literature:
            return dict(run_id=run['run_id'],branch_id=literature['branch_id'],active=[dict(r) for r in modules])
        if run['status']=='CLOSED':
            raise RuntimeError('Goal closed before an active literature researcher could receive the question')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('work',type=Path)
    p.add_argument('--deployment',type=Path,default=ROOT)
    args=p.parse_args()
    work=args.work.resolve()
    end=time.monotonic()+1800
    while time.monotonic()<end:
        obs=observe(work/'state/gx.db')
        if obs:
            break
        time.sleep(2)
    else:
        raise TimeoutError('No live literature branch observed within 30 minutes')
    cmd=[sys.executable,'-B',str(args.deployment/'acceptance/science_capabilities/gx_oauth.py'),
         str(work),'message','--state',str(work/'state'),'--run-id',obs['run_id'],
         '--branch-id',obs['branch_id'],'--message-id','sugar-original-assay-question','--queue-only',
         '--text','请在文献路线核对，最关键的原文实际测量了哪种底物、材料和反应，能否区分直接功能与间接表型？其它路线继续。']
    reply=subprocess.run(cmd,check=True,capture_output=True,text=True)
    print(json.dumps(dict(observation=obs,ingress=json.loads(reply.stdout)),ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
